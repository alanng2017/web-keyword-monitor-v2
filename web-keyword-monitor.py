#!/usr/bin/env python3
"""
web-keyword-monitor — 网页关键词监控 (CLI 核心版)

功能:
- 监控多个网页，检测指定关键词是否出现
- 支持登录态（每个网站独立 cookie）
- 首次运行建立基线，之后只报告新增/消失
- 推送: 飞书 webhook + 钉钉 webhook + 本地日志
- 运行模式: 一次性（cron 调度）或常驻循环

用法:
  python3 web-keyword-monitor.py --config config.yaml [--once] [--interval 3600]
"""

import argparse
import logging
import random
import sys
import time

from wkm_common import DEFAULT_CONFIG, load_config, run_once, setup_logging

LOG = logging.getLogger("wkm")


def main():
    parser = argparse.ArgumentParser(description="网页关键词监控")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="只跑一次 (供 cron 调用)")
    parser.add_argument("--interval", type=int, default=3600, help="常驻模式轮询间隔秒 (默认 3600)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    log_file = cfg.get("log_file", "monitor.log")
    setup_logging(log_file=log_file)

    if args.once:
        run_once(cfg)
        return

    # 常驻模式
    interval = args.interval
    prob = float(cfg.get("update_probability", 1.0))
    LOG.info("常驻模式启动，每 %s 秒轮询一次，更新概率 %.1f。Ctrl+C 退出", interval, prob)
    while True:
        try:
            if random.random() <= prob:
                run_once(cfg)
            else:
                LOG.info("本轮跳过 (更新概率 %.2f)", prob)
        except Exception as e:
            LOG.error("运行异常: %s", e)
        time.sleep(interval)


if __name__ == "__main__":
    main()
