#!/usr/bin/env python3
"""
wkm_common — 网页关键词监控 共享模块

所有核心逻辑集中在此文件:
- 配置/状态读写
- HTTP 抓取 (curl_cffi 指纹模拟 / requests 双引擎, 自动重试+指数退避)
- 关键词匹配
- 飞书/钉钉推送
- 上下文清理
- 日志轮转 (RotatingFileHandler)

爬虫最佳实践 (参考 curl_cffi 官方文档与主流反反爬案例):
1. 持久化 Session: 复用 TLS 握手与 HTTP/2 连接, 降低被识别为脚本的概率
2. impersonate="chrome" (不带版本号): curl_cffi 升级后自动跟进最新浏览器指纹,
   避免硬编码 chrome124 随时间过期
3. 不手动伪造请求头: curl_cffi impersonate 时自动附加与真实浏览器一致的
   头部及顺序 (头顺序本身是指纹的一部分), 手动 setdefault 反而破坏一致性
4. 重试只针对可重试错误: 网络异常 / 429 / 5xx, 指数退避 + 抖动
5. 站点间随机礼貌延迟: 模拟真人浏览节奏, 避免高频触发限流
"""

import base64
import hashlib
import hmac
import json
import logging
import random
import re
import sys
import time
import urllib.parse
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
import yaml

LOG = logging.getLogger("wkm")

# ---------------------------------------------------------------- 默认配置
DEFAULT_CONFIG = {
    "sites": [],
    "notify": {
        "feishu_webhook": "",
        "dingtalk_webhook": "",
        "dingtalk_secret": "",
    },
    "state_file": "state.json",
    "log_file": "monitor.log",
    "interval": 3600,
    "update_probability": 1.0,
    "browser_sim": True,
    "request": {
        "timeout": 30,
        "retries": 3,           # 每个站点最大重试次数 (指数退避)
        "backoff": 2.0,         # 退避基数(秒): 等待 backoff * 2^n + jitter
        "impersonate": "chrome",  # curl_cffi 指纹目标; 不带版本号=自动最新
        "proxy": "",            # 可选代理, 如 http://user:pass@host:port 或 socks5://host:port
        "verify_ssl": True,
        # user_agent 仅在 browser_sim=False 或 curl_cffi 不可用时使用
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    },
    # 站点间随机延迟范围(秒), 模拟真人节奏; 设为 [0,0] 禁用
    "politeness_jitter": [1.0, 3.0],
}


# ---------------------------------------------------------------- 配置
def load_config(path: str, create_default: bool = True) -> dict:
    """加载 YAML 配置, 缺省字段自动补齐。create_default=True 时不存在则创建。"""
    p = Path(path)
    if not p.exists():
        if not create_default:
            return _deep_clone(DEFAULT_CONFIG)
        p.write_text(
            yaml.safe_dump(DEFAULT_CONFIG, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        LOG.info("已生成默认配置文件 %s，请编辑后重新运行", path)
        return _deep_clone(DEFAULT_CONFIG)
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return _merge_defaults(cfg)


def save_config(path: str, cfg: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)


def _deep_clone(d: dict) -> dict:
    return json.loads(json.dumps(d))


def _merge_defaults(cfg: dict) -> dict:
    """递归补齐 cfg 中缺失的 DEFAULT_CONFIG 字段"""
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg or cfg[k] is None:
            cfg[k] = _deep_clone(v)
        elif isinstance(v, dict) and isinstance(cfg[k], dict):
            for sk, sv in v.items():
                if sk not in cfg[k] or cfg[k][sk] is None:
                    cfg[k][sk] = _deep_clone(sv)
    return cfg


# ---------------------------------------------------------------- 状态
def load_state(state_file: str) -> dict:
    p = Path(state_file)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state_file: str, state: dict) -> None:
    Path(state_file).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ---------------------------------------------------------------- Cookies 解析
def parse_cookies(raw) -> dict:
    """支持: 字符串 'a=b; c=d' / dict / None"""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    out: dict[str, str] = {}
    for part in str(raw).split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            out[k] = v
    return out


# ---------------------------------------------------------------- 抓取引擎
# 模块级持久化 Session: 复用连接池与 TLS 会话 (curl_cffi 官方推荐做法)
_cffi_session = None
_CFFI_TRIED = False
_requests_session = None


def _get_cffi():
    """惰性导入 curl_cffi, 模块级缓存"""
    global _cffi_session, _CFFI_TRIED
    if not _CFFI_TRIED:
        _CFFI_TRIED = True
        try:
            from curl_cffi import requests as _cr
            _cffi_session = _cr.Session()
        except ImportError:
            _cffi_session = None
        except Exception as e:
            LOG.warning("curl_cffi 初始化失败, 回退到 requests: %s", e)
            _cffi_session = None
    return _cffi_session


def _get_requests():
    global _requests_session
    if _requests_session is None:
        _requests_session = requests.Session()
        _requests_session.trust_env = False
    return _requests_session


# 可安全重试的 HTTP 状态码: 限流 + 服务端错误
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _is_retryable(err: Exception = None, status: int = None) -> bool:
    """判断错误是否值得重试: 网络层异常 / 429 / 5xx"""
    if status is not None:
        return status in _RETRYABLE_STATUS
    if isinstance(err, (
        requests.ConnectionError,
        requests.Timeout,
        requests.ChunkedEncodingError,
    )):
        return True
    # curl_cffi 的异常继承自 requests 兼容层, 名字不同则按字符串兜底判断
    name = type(err).__name__
    return name in ("CurlError", "RequestsError", "ConnectionError", "Timeout")


def fetch_page(site: dict, req_cfg: dict, browser_sim: bool = True) -> str:
    """
    抓取网页内容, 返回 HTML 文本。失败(重试耗尽后)抛出最后一次异常。

    反反爬策略:
    - browser_sim=True 且 curl_cffi 可用: 用浏览器 TLS/HTTP2 指纹请求,
      headers 由 curl_cffi 自动生成(与真实浏览器一致, 含正确头顺序)
    - 否则回退标准 requests + 显式 UA
    - 可重试错误按指数退避重试: 等待 backoff * 2^attempt + 随机抖动
    """
    headers: dict = {}
    if site.get("headers"):
        headers.update(site["headers"])
    # browser_sim 关闭或 curl_cffi 不可用时才手动指定 UA
    if not (browser_sim and _get_cffi() is not None):
        headers.setdefault("User-Agent", req_cfg.get("user_agent", ""))

    method = site.get("method", "GET").upper()
    url = site["url"]
    timeout = req_cfg.get("timeout", 30)
    verify = req_cfg.get("verify_ssl", True)
    cookies = parse_cookies(site.get("cookies"))
    retries = max(0, int(req_cfg.get("retries", 3)))
    backoff = float(req_cfg.get("backoff", 2.0))
    proxy = req_cfg.get("proxy") or None
    use_cffi = browser_sim and _get_cffi() is not None

    last_err: Exception = None
    for attempt in range(retries + 1):
        if attempt > 0:
            delay = backoff * (2 ** (attempt - 1)) + random.uniform(0, 1)
            LOG.debug("[%s] 第 %d 次重试, 等待 %.1fs", url, attempt, delay)
            time.sleep(delay)
        try:
            if use_cffi:
                kwargs = dict(
                    headers=headers or None,
                    cookies=cookies or None,
                    timeout=timeout,
                    verify=verify,
                    allow_redirects=True,
                )
                if proxy:
                    kwargs["proxy"] = proxy
                resp = _get_cffi().request(
                    method, url, impersonate=req_cfg.get("impersonate", "chrome"),
                    **kwargs,
                )
            else:
                proxies = {"http": proxy, "https": proxy} if proxy else None
                resp = _get_requests().request(
                    method, url, headers=headers, cookies=cookies,
                    timeout=timeout, verify=verify, proxies=proxies,
                )
            if resp.status_code in _RETRYABLE_STATUS and attempt < retries:
                LOG.warning("[%s] HTTP %d, 将重试 (%d/%d)",
                            url, resp.status_code, attempt + 1, retries)
                last_err = RuntimeError(f"HTTP {resp.status_code}")
                continue
            resp.raise_for_status()
            if not use_cffi:
                resp.encoding = resp.apparent_encoding or resp.encoding
            return resp.text
        except Exception as e:
            last_err = e
            if _is_retryable(err=e) and attempt < retries:
                LOG.warning("[%s] 请求异常: %s, 将重试 (%d/%d)",
                            url, e, attempt + 1, retries)
                continue
            raise
    raise last_err or RuntimeError("fetch failed")


# ---------------------------------------------------------------- 关键词
def check_keywords(site: dict, text: str) -> list[str]:
    """检查关键词, 返回命中的关键词列表。"""
    keywords = site.get("keywords") or []
    if not keywords:
        return []
    match_mode = site.get("match_mode", "any")
    case_sensitive = site.get("case_sensitive", False)
    selector = site.get("selector", "") or ""

    scope = text
    if selector:
        m = re.search(selector, text, re.IGNORECASE | re.DOTALL)
        # selector 未命中 → scope 为空, 不再回退全页 (修复原版区域限定失效的 bug)
        scope = m.group(0) if m else ""

    flags = 0 if case_sensitive else re.IGNORECASE
    hits: list[str] = []
    n_valid = 0
    for kw in keywords:
        kw = str(kw).strip()
        if not kw:
            continue
        n_valid += 1
        if scope and re.search(re.escape(kw), scope, flags):
            hits.append(kw)
            if match_mode == "any":
                break
    # all 模式: 必须全部命中才算命中 (否则视为未命中, 修复原版部分命中误报)
    if match_mode == "all" and n_valid and len(hits) < n_valid:
        return []
    return hits


# ---------------------------------------------------------------- 页面指纹
def page_fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


# ---------------------------------------------------------------- 推送
def notify_feishu(webhook: str, text: str) -> bool:
    if not webhook:
        return False
    payload = {
        "msg_type": "post",
        "content": {"zh_cn": {"content": [[{"tag": "md", "text": text}]]}},
    }
    try:
        r = _get_requests().post(webhook, json=payload, timeout=15)
        ok = r.status_code == 200 and r.json().get("code") == 0
        if not ok:
            LOG.warning("飞书推送失败: %s %s", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        LOG.warning("飞书推送异常: %s", e)
        return False


def notify_dingtalk(webhook: str, secret: str, text: str) -> bool:
    if not webhook:
        return False
    try:
        if secret:
            ts = str(round(datetime.now().timestamp() * 1000))
            string_to_sign = f"{ts}\n{secret}"
            hmac_code = hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            webhook = f"{webhook}&timestamp={ts}&sign={sign}"
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": "关键词监控", "text": text},
        }
        r = _get_requests().post(webhook, json=payload, timeout=15)
        ok = r.status_code == 200 and r.json().get("errcode") == 0
        if not ok:
            LOG.warning("钉钉推送失败: %s %s", r.status_code, r.text[:200])
        return ok
    except Exception as e:
        LOG.warning("钉钉推送异常: %s", e)
        return False


def notify_all(cfg: dict, text: str) -> None:
    n = cfg.get("notify") or {}
    ok_f = notify_feishu(n.get("feishu_webhook", ""), text)
    ok_d = notify_dingtalk(
        n.get("dingtalk_webhook", ""), n.get("dingtalk_secret", ""), text
    )
    if not (ok_f or ok_d) and (
        n.get("feishu_webhook") or n.get("dingtalk_webhook")
    ):
        LOG.warning("所有推送通道失败，消息: %s", text)


# ---------------------------------------------------------------- 上下文清理
def clean_snippet(text: str, max_len: int = 120) -> str:
    """清理 HTML 上下文片段: 去标签/实体/base64/碎片, 截断"""
    text = re.sub(r"<[^>]*>", "", text)
    text = re.sub(r"[^>\s]*>", "", text)
    # HTML 实体解码 (简单覆盖常用实体)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;|#\d+;", "", text)
    # base64 / 长无序串
    text = re.sub(r"[A-Za-z0-9+/=_\-]{40,}", "\u2026", text)
    # 行首纯符号
    text = re.sub(r"^[^a-zA-Z0-9\u4e00-\u9fff]+", "", text)
    # 残留 HTML 属性碎片
    text = re.sub(r'\b[a-z]+\s*=\s*["\'][^"\'\s]*', "", text)
    text = re.sub(r'\b[a-z]+=""', "", text)
    text = re.sub(r"\b[a-z]+\s*$", "", text)
    # 行尾孤立符号
    text = re.sub(r'[<>"]\s*$', "", text)
    # 压缩空白
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len] + "\u2026"
    return text


# ---------------------------------------------------------------- 日志
def setup_logging(
    log_file: str | None = None,
    level: int = logging.INFO,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 3,
) -> None:
    """配置 wkm logger: 控制台 + 可选 RotatingFileHandler。幂等安全。"""
    LOG.handlers.clear()
    LOG.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    LOG.addHandler(sh)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        LOG.addHandler(fh)


# ---------------------------------------------------------------- 主逻辑
def run_once(cfg: dict) -> list[str]:
    """
    执行一轮检查。返回本轮的消息摘要列表。
    站点间随机礼貌延迟, 模拟真人浏览节奏。
    """
    state_file = cfg.get("state_file", "state.json")
    state = load_state(state_file)
    req_cfg = cfg.get("request", DEFAULT_CONFIG["request"])
    browser_sim = cfg.get("browser_sim", True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summaries: list[str] = []

    try:
        jitter_lo, jitter_hi = cfg.get("politeness_jitter") or [0, 0]
    except (ValueError, TypeError):
        jitter_lo, jitter_hi = 0, 0

    sites = cfg.get("sites", [])
    for idx, site in enumerate(sites):
        # 礼貌延迟: 非首个站点前随机等待
        if idx > 0 and jitter_hi > 0:
            time.sleep(random.uniform(float(jitter_lo), float(jitter_hi)))

        name = site.get("name", site.get("url", "?"))
        try:
            text = fetch_page(site, req_cfg, browser_sim=browser_sim)
        except Exception as e:
            LOG.error("[%s] 抓取失败: %s", name, e)
            summaries.append(f"[{name}] 抓取失败: {e}")
            continue

        fp = page_fingerprint(text)
        st = state.setdefault(site["url"], {})
        is_first = st.get("first_run", True)
        prev_hits = set(st.get("hits", []))
        hits = check_keywords(site, text)

        # 更新状态
        st["first_run"] = False
        st["hits"] = list(hits)
        st["fingerprint"] = fp
        st["last_check"] = now

        new_hits = [h for h in hits if h not in prev_hits]
        cleared = [h for h in prev_hits if h not in hits]

        if is_first:
            LOG.info(
                "[%s] 首次检查: %s",
                name,
                ", ".join(hits) if hits else "无关键词命中",
            )

        changed = False
        msgs: list[str] = []
        if new_hits:
            msgs.append(f"\U0001f195 新命中: {', '.join(new_hits)}")
            changed = True
        if cleared:
            msgs.append(f"\u2705 已消失: {', '.join(cleared)}")
            changed = True

        if changed:
            # 提取上下文
            snippets: list[str] = []
            for kw in hits:
                idx2 = text.lower().find(kw.lower())
                if idx2 >= 0:
                    start = max(0, idx2 - 80)
                    end = min(len(text), idx2 + len(kw) + 80)
                    snippet = text[start:end].replace("\n", " ").strip()
                    snippet = clean_snippet(snippet)
                    if snippet:
                        snippets.append(snippet)
            lines = [
                "**\U0001f514 关键词监控**",
                f"**站点**: {name}",
                f"**URL**: {site['url']}",
                f"**时间**: {now}",
            ]
            lines.extend(msgs)
            if snippets:
                lines.append("")
                lines.append("**\U0001f4c4 上下文**:")
                for s in snippets[:3]:
                    lines.append(f"> {s}")
            text_msg = "\n".join(lines)
            LOG.info("[%s] %s", name, " | ".join(msgs))
            notify_all(cfg, text_msg)
            summaries.append(f"[{name}] {' | '.join(msgs)}")
        else:
            LOG.info(
                "[%s] 无变化 (命中 %s)",
                name,
                ", ".join(hits) if hits else "无",
            )

    save_state(state_file, state)
    return summaries
