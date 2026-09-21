#!/usr/bin/env python3
"""
fpv-fetcher · 定时调度入口
=========================
两种用法：
  1) 外部 cron 调度（推荐，简单可靠）：
       python scheduler.py -c config.json --once
  2) 内置守护进程（自带 cron）：
       python scheduler.py -c config.json --cron "0 */6 * * *"
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

from croniter import croniter

from fetcher import Fetcher, load_config


def run_once(config_path: str) -> int:
    """跑一轮全部分类，返回本轮下载总数。"""
    cfg = load_config(config_path)
    fetcher = Fetcher(cfg)
    total = 0
    try:
        print(f"=== 抓取开始 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ===")
        for cat in cfg.categories:
            total += fetcher.run_category(cat)
        print(f"=== 完成，本轮共下载 {total} 个视频 ===")
    finally:
        fetcher.close()
    return total


def daemon(config_path: str, cron: str) -> None:
    """按 cron 周期循环运行。"""
    if not croniter.is_valid(cron):
        print(f"非法 cron 表达式: {cron}", file=sys.stderr)
        sys.exit(1)
    it = croniter(cron, datetime.now())
    print(f"调度启动，cron={cron}")
    while True:
        nxt = it.get_next(datetime)
        while True:
            remain = (nxt - datetime.now()).total_seconds()
            if remain <= 0:
                break
            time.sleep(min(60, remain))
        try:
            run_once(config_path)
        except Exception as e:
            print(f"[!] 运行异常: {e}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description="fpv 定时视频抓取器")
    ap.add_argument("-c", "--config", default="config.json", help="配置文件路径")
    ap.add_argument(
        "--once", action="store_true", help="只跑一轮后退出（配合外部 cron 使用）"
    )
    ap.add_argument(
        "--cron", default=None,
        help="内置守护进程模式，传入 cron 表达式（如 '0 */6 * * *'）"
    )
    args = ap.parse_args()

    if args.cron:
        daemon(args.config, args.cron)
    else:
        run_once(args.config)


if __name__ == "__main__":
    main()
