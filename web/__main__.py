"""python -m web -c config.json"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

from fetcher import load_config
from web.app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description="fpv 网页片库（浏览器直出，不转码）")
    ap.add_argument("-c", "--config", default="config.json", help="配置文件路径")
    ap.add_argument("--host", default=None, help="覆盖 web_host")
    ap.add_argument("--port", type=int, default=None, help="覆盖 web_port")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        print(f"找不到配置: {cfg_path}", file=sys.stderr)
        sys.exit(1)
    cfg = load_config(cfg_path)
    if args.host:
        cfg.web_host = args.host
    if args.port:
        cfg.web_port = args.port

    if cfg.r2_bucket:
        print(f"片库源: Cloudflare R2 桶 {cfg.r2_bucket}")
    if cfg.web_password:
        print(f"片库口令已启用，监听 http://{cfg.web_host}:{cfg.web_port}/")
    else:
        print(
            f"[!] 未设置 web_password，片库对能访问 {cfg.web_host}:{cfg.web_port} 的人开放",
            file=sys.stderr,
        )
        print(f"片库启动 http://{cfg.web_host}:{cfg.web_port}/")

    app = create_app(cfg)
    uvicorn.run(app, host=cfg.web_host, port=cfg.web_port, log_level="info")


if __name__ == "__main__":
    main()
