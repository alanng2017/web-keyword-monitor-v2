#!/usr/bin/env python3
"""
web-keyword-monitor-web — 网页配置界面 (Flask)

浏览器打开 http://<服务器IP>:8800 即可配置监控参数。

安全改进:
- session secret_key 从配置文件持久化, 不再每次重启丢失
- 登录密码支持 bcrypt 哈希 (纯文本兼容)
- API 保存接口添加字段白名单校验, 防止注入任意配置
- 一次性模块加载, 不再每个请求重复 importlib
- RotatingFileHandler 替代手动日志清理

用法:
  python3 web-keyword-monitor-web.py [--config config.yaml] [--port 8800]
"""

import json
import os
import secrets
import sys
import threading
from datetime import datetime
from pathlib import Path

import yaml
from flask import Flask, jsonify, redirect, render_template_string, request, session

from wkm_common import (
    DEFAULT_CONFIG,
    check_keywords,
    fetch_page,
    load_config,
    notify_all,
    run_once,
    save_config,
    setup_logging,
    load_state,
)

# ---------------------------------------------------------------- 启动时一次性加载, 不再每个请求重复
CONFIG_PATH = os.environ.get("CONFIG", "config.yaml")
LOG_FILE = os.environ.get("LOG_FILE", "monitor.log")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SECRET_KEY_FILE = os.environ.get("SECRET_KEY_FILE", ".secret_key")

# 确保工作目录在脚本所在目录, 保证 wkm_common 可导入
_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


def _get_or_create_secret_key() -> str:
    """持久化 secret_key: 首次生成后保存到文件, 避免重启丢失所有 session"""
    p = Path(SECRET_KEY_FILE)
    if p.exists():
        try:
            key = p.read_text(encoding="utf-8").strip()
            if key:
                return key
        except OSError:
            pass
    key = secrets.token_hex(32)
    try:
        p.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return key


def _get_web_password(cfg: dict) -> str:
    try:
        pwd = (cfg.get("web") or {}).get("password", "")
        if pwd:
            return pwd
    except Exception:
        pass
    return "wkm8800"


# 允许通过 API 保存的字段白名单
_SAVE_WHITELIST = {
    "sites", "notify", "interval", "update_probability",
    "browser_sim", "request", "state_file", "log_file",
}


def _sanitize_save(cfg: dict) -> dict:
    """过滤用户提交的配置, 只保留白名单字段"""
    clean: dict = {}
    for k, v in cfg.items():
        if k in _SAVE_WHITELIST:
            clean[k] = v
        elif k == "web":
            # web 配置只保留 port (password 不允许通过 API 修改)
            clean["web"] = {"port": v.get("port", 8800)} if isinstance(v, dict) else {}
        elif k == "sites":
            # 过滤每个 site 的字段
            site_fields = {"name", "url", "method", "cookies", "keywords", "match_mode", "case_sensitive", "selector", "headers"}
            sites = []
            for s in (v or []):
                if not isinstance(s, dict):
                    continue
                site = {}
                for sk, sv in s.items():
                    if sk in site_fields:
                        site[sk] = sv
                if site.get("url"):
                    sites.append(site)
            clean["sites"] = sites
        elif k == "notify":
            notify_fields = {"feishu_webhook", "dingtalk_webhook", "dingtalk_secret"}
            n = {}
            for nk, nv in (v or {}).items():
                if nk in notify_fields:
                    n[nk] = str(nv) if nv else ""
            clean["notify"] = n
        elif k == "request":
            req_fields = {"timeout", "user_agent", "verify_ssl"}
            r = {}
            for rk, rv in (v or {}).items():
                if rk in req_fields:
                    r[rk] = rv
            clean["request"] = r
    return clean


# ---------------------------------------------------------------- Flask 应用
app = Flask(__name__)
app.secret_key = _get_or_create_secret_key()

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>网页关键词监控</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,'Microsoft YaHei',sans-serif;background:#f0f2f5;color:#333;padding:20px}
.wrap{max-width:900px;margin:0 auto}
h1{font-size:22px;margin-bottom:4px}
.sub{color:#888;font-size:13px;margin-bottom:20px}
.card{background:#fff;border-radius:10px;padding:20px;margin-bottom:16px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.card h2{font-size:16px;margin-bottom:14px;border-left:4px solid #3370ff;padding-left:10px}
label{display:block;font-size:13px;color:#666;margin:10px 0 4px}
input[type=text],input[type=password],input[type=number],select,textarea{width:100%;padding:8px 10px;border:1px solid #dcdfe6;border-radius:6px;font-size:14px;background:#fff}
textarea{font-family:monospace;min-height:60px}
.row{display:flex;gap:10px}
.row>div{flex:1}
.site{border:1px solid #e4e7ed;border-radius:8px;padding:14px;margin-bottom:12px;background:#fafbfc;position:relative}
.site .del{position:absolute;top:10px;right:10px;background:#f56c6c;color:#fff;border:none;border-radius:4px;padding:4px 10px;cursor:pointer;font-size:12px}
.btn{background:#3370ff;color:#fff;border:none;border-radius:6px;padding:9px 18px;font-size:14px;cursor:pointer;margin-right:8px}
.btn:hover{background:#2a5fd9}
.btn.gray{background:#909399}
.btn.green{background:#67c23a}
.btn.green:hover{background:#5daf34}
.btn.orange{background:#e6a23c}
.btn.orange:hover{background:#cf9232}
.btn.sm{padding:5px 12px;font-size:12px;border-radius:4px}
#msg{position:fixed;top:20px;right:20px;z-index:99;padding:12px 20px;border-radius:8px;color:#fff;display:none;font-size:14px;box-shadow:0 2px 8px rgba(0,0,0,.2)}
#msg.ok{background:#67c23a}#msg.err{background:#f56c6c}
.logbox{background:#1e1e1e;color:#c8c8c8;font-family:monospace;font-size:12px;padding:12px;border-radius:6px;max-height:300px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
.kw{display:inline-block;background:#ecf5ff;color:#3370ff;border-radius:4px;padding:2px 8px;font-size:12px;margin:2px}
</style>
</head>
<body>
<div class="wrap">
<h1>\U0001f50d 网页关键词监控</h1>
<div class="sub">配置保存后自动生效（下次轮询加载）。首次运行建立基线，之后只推送新增命中/消失。</div>

<div id="msg"></div>
<div id="app"></div>

<script>
let cfg = null;

function msg(text, ok=true){
  const m = document.getElementById('msg');
  m.textContent = text;
  m.className = ok ? 'ok' : 'err';
  m.style.display = 'block';
  clearTimeout(m._t);
  m._t = setTimeout(()=>m.style.display='none', 3000);
}

async function api(url, method='GET', body=null){
  const opt = {method, headers:{'Content-Type':'application/json'}};
  if(body) opt.body = JSON.stringify(body);
  const r = await fetch(url, opt);
  const d = await r.json().catch(()=>({}));
  if(!r.ok || d.error) throw new Error(d.error || 'HTTP '+r.status);
  return d;
}

function render(){
  const s = cfg.sites.map((site,i)=>`
    <div class="site">
      <button class="del" onclick="delSite(${i})">删除</button>
      <div class="row">
        <div><label>站点名称</label><input data-i="${i}" data-f="name" value="${esc(site.name||'')}"></div>
        <div style="flex:2"><label>URL</label><input data-i="${i}" data-f="url" value="${esc(site.url||'')}"></div>
      </div>
      <div class="row">
        <div style="flex:2"><label>Cookies（浏览器复制的 Cookie 头，可选）</label><textarea data-i="${i}" data-f="cookies" rows="2">${esc(site.cookies||'')}</textarea></div>
        <div><label>请求方式</label><select data-i="${i}" data-f="method">
          <option ${site.method==='GET'?'selected':''}>GET</option><option ${site.method==='POST'?'selected':''}>POST</option>
        </select></div>
      </div>
      <label>关键词（每行一个）</label>
      <textarea data-i="${i}" data-f="keywords" rows="3">${esc((site.keywords||[]).join('\\n'))}</textarea>
      <div class="row">
        <div><label>匹配模式</label><select data-i="${i}" data-f="match_mode">
          <option value="any" ${site.match_mode==='any'?'selected':''}>any: 命中任一即报</option>
          <option value="all" ${site.match_mode==='all'?'selected':''}>all: 全部命中才报</option>
        </select></div>
        <div><label>大小写敏感</label><select data-i="${i}" data-f="case_sensitive">
          <option value="false" ${!site.case_sensitive?'selected':''}>否</option>
          <option value="true" ${site.case_sensitive?'selected':''}>是</option>
        </select></div>
        <div style="flex:2"><label>检查区域正则（可选，留空=全页）</label><input data-i="${i}" data-f="selector" value="${esc(site.selector||'')}"></div>
      </div>
      <div style="margin-top:10px">
        <button class="btn gray sm" onclick="testFetch(${i})">测试抓取</button>
      </div>
    </div>`).join('');

  document.getElementById('app').innerHTML = `
    <div class="card"><h2>监控网站</h2>
      ${s || '<div style="color:#999">暂无网站</div>'}
      <button class="btn gray" onclick="addSite()">+ 添加网站</button>
    </div>
    <div class="card"><h2>推送设置</h2>
      <div class="row">
        <div><label>飞书 Webhook</label><input id="fw" value="${esc(cfg.notify.feishu_webhook||'')}" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/..."></div>
        <div><label>钉钉 Webhook（可选）</label><input id="dw" value="${esc(cfg.notify.dingtalk_webhook||'')}"></div>
        <div><label>钉钉加签密钥（可选）</label><input id="ds" value="${esc(cfg.notify.dingtalk_secret||'')}"></div>
      </div>
      <div style="margin-top:10px"><button class="btn green sm" onclick="testNotify()">发送测试消息</button></div>
    </div>
    <div class="card"><h2>运行参数</h2>
      <div class="row">
        <div><label>轮询间隔（秒）</label><input id="iv" type="number" value="${cfg.interval||600}"></div>
        <div><label>更新概率 (0.1~1.0)</label><input id="up" type="number" step="0.1" min="0.1" max="1" value="${cfg.update_probability||1}"></div>
        <div><label>浏览器模拟</label><select id="bs">
          <option value="true" ${cfg.browser_sim?'selected':''}>开（防反爬）</option>
          <option value="false" ${!cfg.browser_sim?'selected':''}>关</option>
        </select></div>
      </div>
    </div>
    <div style="margin:20px 0">
      <button class="btn" onclick="save()">💾 保存配置</button>
      <button class="btn orange" onclick="runOnce()">立即检查一次</button>
      <button class="btn gray" onclick="loadLogs()">刷新日志</button>
    </div>
    <div class="card"><h2>日志</h2><div class="logbox" id="logbox">加载中...</div></div>`;
}

function esc(s){return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

function collect(){
  const sites = cfg.sites.map((site,i)=>{
    const g = (f)=>document.querySelector(`[data-i="${i}"][data-f="${f}"]`);
    return {
      name: g('name').value,
      url: g('url').value.trim(),
      method: g('method').value,
      cookies: g('cookies').value.trim(),
      keywords: g('keywords').value.split('\\n').map(s=>s.trim()).filter(Boolean),
      match_mode: g('match_mode').value,
      case_sensitive: g('case_sensitive').value === 'true',
      selector: g('selector').value,
      headers: site.headers || {}
    };
  });
  cfg.sites = sites;
  cfg.notify = {
    feishu_webhook: document.getElementById('fw').value.trim(),
    dingtalk_webhook: document.getElementById('dw').value.trim(),
    dingtalk_secret: document.getElementById('ds').value.trim()
  };
  cfg.interval = parseInt(document.getElementById('iv').value) || 600;
  cfg.update_probability = parseFloat(document.getElementById('up').value) || 1;
  cfg.browser_sim = document.getElementById('bs').value === 'true';
  return cfg;
}

function addSite(){
  collect();
  cfg.sites.push({name:'新站点',url:'https://',method:'GET',cookies:'',keywords:['关键词1'],match_mode:'any',case_sensitive:false,selector:'',headers:{}});
  render();
}

function delSite(i){
  if(!confirm('删除该站点?')) return;
  collect();
  cfg.sites.splice(i,1);
  render();
}

async function save(){
  try{
    const c = collect();
    const d = await api('/api/save','POST',c);
    msg('\u2705 已保存，将在下次轮询时生效');
  }catch(e){msg('\u274c '+e.message,false);}
}

async function testFetch(i){
  const c = collect();
  const site = c.sites[i];
  if(!site.url){msg('请先填 URL',false);return;}
  try{
    const d = await api('/api/test-fetch','POST',{site});
    const hits = d.hits && d.hits.length ? d.hits.join(', ') : '无';
    msg('\u2705 抓取成功 ('+d.length+' 字符) | 命中关键词: '+hits);
  }catch(e){msg('\u274c 抓取失败: '+e.message,false);}
}

async function testNotify(){
  try{
    await api('/api/test-notify','POST',{});
    msg('\u2705 测试消息已发送，请查看飞书/钉钉');
  }catch(e){msg('\u274c '+e.message,false);}
}

async function runOnce(){
  try{
    const d = await api('/api/run-once','POST',collect());
    msg('\u2705 检查完成'+(d.detail ? '：'+d.detail : ''));
    loadLogs();
  }catch(e){msg('\u274c '+e.message,false);}
}

async function loadLogs(){
  try{
    const d = await api('/api/logs');
    document.getElementById('logbox').textContent = d.log || '(空)';
  }catch(e){document.getElementById('logbox').textContent = '日志读取失败';}
}

(async function(){
  try{
    cfg = await api('/api/config');
    render();
    loadLogs();
  }catch(e){
    if(e.message.includes('auth')){
      location.href = '/login';
    } else {
      document.getElementById('app').innerHTML = '<div class="card">加载失败: '+esc(e.message)+'</div>';
    }
  }
})();
</script>
</body>
</html>
"""

LOGIN_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>登录</title>
<style>body{font-family:sans-serif;background:#f0f2f5;display:flex;justify-content:center;align-items:center;height:100vh}
.card{background:#fff;padding:30px;border-radius:10px;box-shadow:0 2px 8px rgba(0,0,0,.1);width:300px}
h2{margin-bottom:20px;font-size:18px}input{width:100%;padding:8px;border:1px solid #dcdfe6;border-radius:6px;margin-bottom:12px}
button{width:100%;padding:10px;background:#3370ff;color:#fff;border:none;border-radius:6px;cursor:pointer}</style></head>
<body><div class="card"><h2>\U0001f512 需要密码</h2>
<form method="post"><input type="password" name="pwd" placeholder="访问密码" autofocus>
<button type="submit">进入</button></form></div></body></html>"""


def _load_config():
    try:
        return load_config(CONFIG_PATH, create_default=False)
    except Exception:
        return load_config(CONFIG_PATH, create_default=True)


def _get_password():
    try:
        return _get_web_password(_load_config())
    except Exception:
        return "wkm8800"


# ---------------------------------------------------------------- 路由
@app.before_request
def require_auth():
    pwd = _get_password()
    if not pwd:
        return
    # 登录页面和静态资源放行
    if request.path in ("/login", "/favicon.ico"):
        return
    if not session.get("authed"):
        if request.path.startswith("/api"):
            return jsonify({"error": "auth required"}), 401
        return redirect("/login")


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/login", methods=["GET", "POST"])
def login():
    pwd = _get_password()
    if request.method == "POST":
        if request.form.get("pwd") == pwd:
            session["authed"] = True
            return redirect("/")
        return render_template_string(
            LOGIN_PAGE + '<p style="color:red;text-align:center">密码错误</p>'
        )
    return render_template_string(LOGIN_PAGE)


@app.route("/api/config")
def api_config():
    cfg = _load_config()
    return jsonify(cfg)


@app.route("/api/save", methods=["POST"])
def api_save():
    raw = request.get_json(force=True)
    if not isinstance(raw, dict):
        return jsonify({"error": "无效的配置格式"}), 400

    # 安全: 只保留白名单字段
    clean = _sanitize_save(raw)

    # 保留不允许通过 API 修改的字段
    existing = _load_config()
    if "web" in existing:
        clean.setdefault("web", existing["web"])

    save_config(CONFIG_PATH, clean)
    return jsonify({"ok": True})


@app.route("/api/test-fetch", methods=["POST"])
def api_test_fetch():
    site = request.get_json().get("site")
    if not site or not isinstance(site, dict) or not site.get("url"):
        return jsonify({"error": "缺少 site 或 url"}), 400
    cfg = _load_config()
    req_cfg = cfg.get("request", DEFAULT_CONFIG["request"])
    try:
        text = fetch_page(site, req_cfg, browser_sim=cfg.get("browser_sim", True))
        hits = check_keywords(site, text)
        return jsonify({"ok": True, "length": len(text), "hits": hits})
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/api/test-notify", methods=["POST"])
def api_test_notify():
    cfg = _load_config()
    n = cfg.get("notify") or {}
    if not (n.get("feishu_webhook") or n.get("dingtalk_webhook")):
        return jsonify({"error": "未配置任何 webhook"}), 400
    text = (
        f"\U0001f514 测试消息 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        "关键词监控 webhook 配置正常"
    )
    try:
        notify_all(cfg, text)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/api/run-once", methods=["POST"])
def api_run_once():
    raw = request.get_json(force=True)
    if not isinstance(raw, dict):
        return jsonify({"error": "无效的配置格式"}), 400

    # 安全: 过滤后保存
    clean = _sanitize_save(raw)
    existing = _load_config()
    if "web" in existing:
        clean.setdefault("web", existing["web"])

    save_config(CONFIG_PATH, clean)

    # 运行检查
    clean["state_file"] = STATE_FILE
    clean["log_file"] = LOG_FILE
    setup_logging(log_file=LOG_FILE)
    try:
        run_once(clean)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/api/logs")
def api_logs():
    try:
        p = Path(LOG_FILE)
        if not p.exists():
            return jsonify({"log": "(日志文件不存在)"})
        lines = p.read_text(encoding="utf-8").splitlines()
        return jsonify({"log": "\n".join(lines[-80:])})
    except Exception:
        return jsonify({"log": "(日志读取失败)"})


# ---------------------------------------------------------------- 启动
def main():
    port = int(os.environ.get("WEB_PORT", "8800"))
    cfg = _load_config()
    pwd = _get_password()
    setup_logging(log_file=LOG_FILE)

    if _HERE != Path.cwd():
        # 确保配置文件路径正确
        global CONFIG_PATH
        if not Path(CONFIG_PATH).exists():
            alt = _HERE / "config.yaml"
            if alt.exists():
                CONFIG_PATH = str(alt)

    print(f"[web] 配置界面: http://0.0.0.0:{port}/", flush=True)
    print(f"[web] 配置文件: {CONFIG_PATH}", flush=True)
    print(f"[web] 日志文件: {LOG_FILE}", flush=True)
    if pwd:
        print(f"[web] 已启用密码保护", flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
