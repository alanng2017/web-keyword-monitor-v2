#!/usr/bin/env python3
"""
web-keyword-monitor-ui — 网页关键词监控【交互版】

功能:
- 交互菜单管理: 监控网站 / 关键词 / cookies / 推送 webhook / 轮询间隔 / 状态文件
- 与 web-keyword-monitor.py 共用同一 config.yaml, 可互相切换使用
- 添加/编辑网站后可立即测试抓取, 验证 cookies 是否有效
- 可发送测试消息验证飞书/钉钉 webhook
- 支持命令行直跑: --once / --interval N (headless, 与核心版行为一致)

用法:
  python3 web-keyword-monitor-ui.py                 # 进入交互菜单
  python3 web-keyword-monitor-ui.py --once          # 立即检查一次
  python3 web-keyword-monitor-ui.py --interval 600   # 常驻轮询
"""

import argparse
import random
import sys
import time

from datetime import datetime

from wkm_common import (
    DEFAULT_CONFIG,
    check_keywords,
    fetch_page,
    load_config,
    notify_all,
    run_once,
    save_config,
    setup_logging,
)

LOG = logging.getLogger("wkm-ui")


# ---------------------------------------------------------------- 交互菜单辅助
def prompt(label: str, default: str = "", secret: bool = False) -> str:
    if default != "":
        label = f"{label} [{default}]"
    if secret:
        import getpass
        val = getpass.getpass(f"{label}: ").strip()
    else:
        val = input(f"{label}: ").strip()
    if not val:
        val = default
    return val


def menu_show(title: str, options: list, footer: str = "") -> int:
    print(f"\n===== {title} =====")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    if footer:
        print(f"  {footer}")
    try:
        return int(input("请选择: ").strip() or "0")
    except ValueError:
        return 0


def fmt_cookies(c) -> str:
    if not c:
        return "(无)"
    if isinstance(c, dict):
        return "; ".join(f"{k}=***" for k in c)
    return str(c)[:60] + ("\u2026" if len(str(c)) > 60 else "")


def fmt_site(s: dict) -> str:
    kw = ", ".join(map(str, s.get("keywords") or []))
    mode = s.get("match_mode", "any")
    return (
        f"[{s.get('name', '?')}] {s.get('url')}\n"
        f"     关键词: {kw} | 模式: {mode} | cookies: {fmt_cookies(s.get('cookies'))}"
    )


def edit_site_fields(s: dict, is_new: bool = False) -> dict:
    print("\n---- 网站设置 (直接回车 = 保持默认) ----")
    s["name"] = prompt("名称", s.get("name", "") or "")
    s["url"] = prompt("URL", s.get("url", "") or "")
    s["method"] = prompt("请求方法 (GET)", s.get("method", "GET") or "GET").upper()
    if s["method"] not in ("GET", "POST"):
        s["method"] = "GET"
    raw = prompt(
        "Cookies (浏览器复制的 Cookie 头粘贴, 可留空)",
        fmt_cookies(s.get("cookies")) if is_new else "",
    )
    if raw or is_new:
        s["cookies"] = raw if raw and raw != "(无)" else ""
    kw = prompt(
        "关键词 (逗号分隔, 如: 停电,维护,紧急)",
        ", ".join(map(str, s.get("keywords") or [])),
    )
    if kw:
        s["keywords"] = [k.strip() for k in kw.split(",") if k.strip()]
    s["match_mode"] = prompt(
        "匹配模式 (any=任一命中/all=全部命中)",
        s.get("match_mode", "any") or "any",
    )
    if s["match_mode"] not in ("any", "all"):
        s["match_mode"] = "any"
    sel = prompt(
        '限定区域正则 (留空=全页, 如: <div id="notice">.*?</div> 需单行)',
        s.get("selector", "") or "",
    )
    if sel != s.get("selector", ""):
        s["selector"] = sel
    cs = prompt("大小写敏感? (y/n)", "y" if s.get("case_sensitive") else "n").lower()
    s["case_sensitive"] = cs.startswith("y")
    return s


def test_site(s: dict, cfg: dict) -> None:
    try:
        text = fetch_page(
            s,
            cfg.get("request", DEFAULT_CONFIG["request"]),
            browser_sim=cfg.get("browser_sim", True),
        )
        print(f"[\u2713] 抓取成功, 页面大小: {len(text)} 字符")
        hits = check_keywords(s, text)
        if hits:
            print(f"[\u2713] 关键词命中: {', '.join(hits)}")
        else:
            print("[i] 未命中任何关键词 (页面可访问, 但关键词未出现, 或 cookies 未生效)")
    except Exception as e:
        print(f"[\u2717] 抓取失败: {e}\n    检查 URL / cookies / 网络")


# ---------------------------------------------------------------- 菜单
def site_menu(cfg: dict, path: str) -> None:
    while True:
        sites = cfg.get("sites", [])
        print(f"\n===== 监控网站管理 (当前 {len(sites)} 个) =====")
        for i, s in enumerate(sites, 1):
            print(f"  {i}. {fmt_site(s)}")
        c = menu_show(
            "网站管理",
            ["查看/编辑网站", "添加网站", "删除网站", "返回主菜单"],
            footer="输入网站编号可快速编辑",
        )
        if c == 0:
            return
        if c == 1:
            if not sites:
                print("[i] 暂无网站, 请先添加")
                continue
            n = menu_show(
                "选择要编辑的网站",
                [s.get("name", s.get("url", "?")) for s in sites],
            )
            if 1 <= n <= len(sites):
                edit_site_fields(sites[n - 1])
                save_config(path, cfg)
        elif c == 2:
            s = edit_site_fields(
                {
                    "name": "", "url": "", "method": "GET", "cookies": "",
                    "keywords": [], "match_mode": "any", "selector": "",
                    "case_sensitive": False,
                },
                is_new=True,
            )
            if not s["url"]:
                print("[!] URL 必填, 已取消添加")
                continue
            sites.append(s)
            save_config(path, cfg)
            t = prompt("\n是否立即测试抓取该网站? (y/n)", "y").lower()
            if t.startswith("y"):
                test_site(s, cfg)
        elif c == 3:
            if not sites:
                print("[i] 暂无网站")
                continue
            n = menu_show(
                "选择要删除的网站",
                [s.get("name", s.get("url", "?")) for s in sites],
            )
            if 1 <= n <= len(sites):
                del sites[n - 1]
                save_config(path, cfg)
                print("[\u2713] 已删除")
        else:
            print("[!] 无效选择")


def notify_menu(cfg: dict, path: str) -> None:
    n = cfg.get("notify", {})
    while True:
        print(f"\n===== 推送设置 =====")
        print(f"  飞书 webhook : {n.get('feishu_webhook') or '(未设置)'}")
        print(f"  钉钉 webhook : {n.get('dingtalk_webhook') or '(未设置)'}")
        print(f"  钉钉加签密钥 : {n.get('dingtalk_secret') or '(未设置)'}")
        c = menu_show(
            "推送设置",
            ["修改飞书 webhook", "修改钉钉 webhook", "修改钉钉加签密钥", "发送测试消息", "返回主菜单"],
        )
        if c == 1:
            n["feishu_webhook"] = prompt("飞书 webhook URL", n.get("feishu_webhook", "") or "")
            save_config(path, cfg)
        elif c == 2:
            n["dingtalk_webhook"] = prompt("钉钉 webhook URL", n.get("dingtalk_webhook", "") or "")
            save_config(path, cfg)
        elif c == 3:
            n["dingtalk_secret"] = prompt("钉钉加签密钥 (SEC 开头)", n.get("dingtalk_secret", "") or "")
            save_config(path, cfg)
        elif c == 4:
            text = f"\U0001f514 测试消息 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n关键词监控 webhook 配置正常"
            print("[i] 发送中...")
            notify_all(cfg, text)
            print("[\u2713] 已发送, 请查看飞书/钉钉")
        elif c == 0:
            return
        else:
            print("[!] 无效选择")


def run_menu(cfg: dict, path: str) -> None:
    while True:
        print(f"\n===== 运行参数 =====")
        print(f"  轮询间隔    : {cfg.get('interval', 3600)} 秒")
        prob = cfg.get("update_probability", 1.0)
        print(f"  更新概率    : {prob}  (每次轮询实际抓取的概率, 模拟真人随机访问)")
        bs = cfg.get("browser_sim", True)
        print(f"  浏览器模拟  : {'开 (Chrome TLS指纹+完整头, 防反爬)' if bs else '关 (标准requests)'}")
        print(f"  状态文件    : {cfg.get('state_file', 'state.json')}")
        print(f"  日志文件    : {cfg.get('log_file', 'monitor.log')}")
        c = menu_show(
            "运行参数",
            ["修改轮询间隔(秒)", "修改更新概率(0.1~1.0)", "切换浏览器模拟 开/关", "修改状态文件", "修改日志文件", "返回主菜单"],
        )
        if c == 1:
            v = prompt("轮询间隔(秒)", str(cfg.get("interval", 3600)))
            if v.isdigit() and int(v) > 0:
                cfg["interval"] = int(v)
                save_config(path, cfg)
            else:
                print("[!] 无效间隔")
        elif c == 2:
            v = prompt("更新概率 (0.1~1.0, 1.0=每次都检查)", str(prob))
            try:
                p = float(v)
                if 0.1 <= p <= 1.0:
                    cfg["update_probability"] = p
                    save_config(path, cfg)
                else:
                    print("[!] 需在 0.1 ~ 1.0 之间")
            except ValueError:
                print("[!] 无效数值")
        elif c == 3:
            cfg["browser_sim"] = not bs
            save_config(path, cfg)
            print(f"[\u2713] 浏览器模拟: {'开' if cfg['browser_sim'] else '关'}")
        elif c == 4:
            cfg["state_file"] = prompt("状态文件路径", cfg.get("state_file", "state.json"))
            save_config(path, cfg)
        elif c == 5:
            cfg["log_file"] = prompt("日志文件路径", cfg.get("log_file", "monitor.log"))
            save_config(path, cfg)
        elif c == 0:
            return
        else:
            print("[!] 无效选择")


def main_menu(cfg: dict, path: str) -> None:
    while True:
        n_sites = len(cfg.get("sites", []))
        print("\n======================================")
        print("  网页关键词监控 — 交互控制台")
        print("======================================")
        print(
            f"  监控网站: {n_sites} 个 | "
            f"飞书: {'\u2713' if cfg['notify'].get('feishu_webhook') else '\u2717'} | "
            f"钉钉: {'\u2713' if cfg['notify'].get('dingtalk_webhook') else '\u2717'}"
        )
        c = menu_show(
            "主菜单",
            ["监控网站管理", "推送设置", "运行参数", "立即检查一次", "启动常驻监控 (Ctrl+C 退出)", "保存并退出"],
        )
        if c == 1:
            site_menu(cfg, path)
        elif c == 2:
            notify_menu(cfg, path)
        elif c == 3:
            run_menu(cfg, path)
        elif c == 4:
            run_once(cfg)
        elif c == 5:
            interval = int(cfg.get("interval", 3600))
            prob = float(cfg.get("update_probability", 1.0))
            print(f"[i] 常驻模式启动, 每 {interval} 秒轮询一次, 更新概率 {prob}. Ctrl+C 退出")
            try:
                while True:
                    if random.random() <= prob:
                        run_once(cfg)
                    else:
                        print(f"[i] 本轮跳过 (更新概率 {prob}), 下一轮 {interval} 秒后")
                    time.sleep(interval)
            except KeyboardInterrupt:
                print("\n[i] 已停止")
                return
        elif c == 6:
            save_config(path, cfg)
            print("[\u2713] 已保存, 再见")
            sys.exit(0)
        else:
            print("[!] 无效选择")


def main():
    parser = argparse.ArgumentParser(description="网页关键词监控 (交互版)")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="立即检查一次 (headless)")
    parser.add_argument("--interval", type=int, default=0, help="常驻轮询间隔秒 (headless)")
    args = parser.parse_args()
    cfg = load_config(args.config)

    if args.once:
        setup_logging(log_file=cfg.get("log_file", "monitor.log"))
        run_once(cfg)
        return

    if args.interval:
        cfg["interval"] = args.interval
        prob = float(cfg.get("update_probability", 1.0))
        setup_logging(log_file=cfg.get("log_file", "monitor.log"))
        print(f"[i] 常驻模式, 每 {args.interval} 秒轮询, 更新概率 {prob}. Ctrl+C 退出")
        try:
            while True:
                if random.random() <= prob:
                    run_once(cfg)
                else:
                    print(f"[i] 本轮跳过 (更新概率 {prob}), {args.interval} 秒后再试")
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\n[i] 已停止")
        return

    print("======================================")
    print("  网页关键词监控 — 交互控制台")
    print("  配置: " + __import__("os").path.abspath(args.config))
    print("======================================")
    try:
        main_menu(cfg, args.config)
    except KeyboardInterrupt:
        print("\n[i] 已退出")


if __name__ == "__main__":
    main()
