# 🔍 web-keyword-monitor v2

网页关键词监控工具 — 定时抓取指定网页，检测关键词的出现/消失，通过飞书/钉钉机器人推送通知。

针对需要盯着某个页面等消息的场景：抢购预告、停服公告、政策更新、论坛新帖……

## ✨ 特性

- **三种使用方式**：CLI（cron 调度）/ 终端交互菜单 / 浏览器 Web 配置界面
- **反反爬**：[curl_cffi](https://curl-cffi.readthedocs.io/) 浏览器 TLS/HTTP2 指纹模拟（`impersonate="chrome"` 自动跟进最新版本），不可用时自动回退标准 requests
- **自动重试**：网络错误/429/5xx 按指数退避 + 抖动重试，仅重试值得重试的错误
- **礼貌抓取**：站点间随机延迟，模拟真人浏览节奏
- **基线通知**：首次运行建立基线，之后只推送「新增命中/已消失」，不重复轰炸
- **登录态支持**：每站点独立 Cookie，直接粘贴浏览器 Cookie 头即可
- **灵活匹配**：任意/全部关键词模式、大小写开关、正则限定检查区域
- **消息带上下文**：命中关键词前后各 80 字符，自动清理 HTML 标签/实体/base64 乱码
- **运维友好**：RotatingFileHandler 日志轮转、状态持久化、Docker 一键部署

## 📦 安装

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml
# 编辑 config.yaml 填入你的站点/关键词/webhook
```

> Python 3.9+。`curl_cffi` 为可选增强，安装后自动启用浏览器指纹模拟。

## 🚀 使用

### CLI（推荐配合 cron / 计划任务）

```bash
# 检查一次即退出 (适合 crontab 调度)
python web-keyword-monitor.py --config config.yaml --once

# 常驻模式, 每 600 秒轮询
python web-keyword-monitor.py --config config.yaml --interval 600
```

### 交互菜单

```bash
python web-keyword-monitor-ui.py
```

向导式管理站点/关键词/Cookie/推送，支持即时测试抓取与测试推送。

### Web 配置界面

```bash
python web-keyword-monitor-web.py
# 浏览器打开 http://localhost:8800
```

⚠️ 生产环境请务必修改 `config.yaml` 中的 `web.password`。

### Docker

```bash
mkdir -p data
docker compose up -d            # 同时启动监控核心 + Web 界面(:8800)
```

## ⚙️ 配置说明

完整字段见 [`config.example.yaml`](config.example.yaml)，核心结构：

```yaml
sites:
  - name: 恩山论坛
    url: https://www.right.com.cn
    cookies: "从浏览器复制的 Cookie 头"
    keywords: [N1, 斐讯]
    match_mode: any        # 任一命中即报
notify:
  feishu_webhook: https://open.feishu.cn/open-apis/bot/v2/hook/xxx
request:
  retries: 3               # 网络错误/429/5xx 自动重试
  impersonate: chrome      # curl_cffi 指纹, 自动最新版
politeness_jitter: [1.0, 3.0]  # 站点间随机延迟
```

## 🏗️ 架构

```
wkm_common.py               # 共享核心: 抓取/匹配/推送/状态/日志
├── web-keyword-monitor.py     # CLI 入口 (--once / --interval)
├── web-keyword-monitor-ui.py  # 终端交互菜单
└── web-keyword-monitor-web.py # Flask Web 配置界面
```

所有入口共用同一 `config.yaml` 与核心逻辑，可随意切换。

## 🔒 安全须知

- `config.yaml` / `state.json` / `.secret_key` 已列入 `.gitignore`，**不要**提交到仓库
- Web 界面密码默认 `wkm8800`，部署后立即修改
- Cookie 与 webhook 等同账号凭证，注意保管

## 📄 License

MIT
