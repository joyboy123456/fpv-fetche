"""把 /data/videos 里的成片和封面迁到 Cloudflare R2。

  python -m web.migrate_r2 -c config.json
  python -m web.migrate_r2 -c config.json --keep-local
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from fetcher import load_config
from web.library import scan
from web.r2 import R2Store
from web.thumbs import ThumbStore


def main() -> None:
    ap = argparse.ArgumentParser(description="本地片库 → Cloudflare R2")
    ap.add_argument("-c", "--config", default="config.json")
    ap.add_argument(
        "--keep-local",
        action="store_true",
        help="上传后保留本地文件（默认校验大小一致后删除以腾盘）",
    )
    args = ap.parse_args()
    cfg = load_config(args.config)
    r2 = R2Store.from_config(cfg)
    if r2 is None:
        print("config 里还没有 r2_account_id / r2_access_key / r2_secret / r2_bucket", file=sys.stderr)
        sys.exit(1)
    thumbs = ThumbStore(cfg.download_root)
    videos = scan(cfg.download_root)
    print(f"本地 {len(videos)} 部，开始上传到桶 {cfg.r2_bucket}")
    uploaded = 0
    skipped = 0
    deleted = 0
    for item in videos:
        local = cfg.download_root / item.relpath
        remote = r2.head_size(item.relpath)
        if remote == item.size:
            print(f"  [=] {item.relpath}")
            skipped += 1
        else:
            print(f"  [↑] {item.relpath} ({item.size / 1048576:.1f} MiB)")
            r2.upload_file(local, item.relpath, "video/mp4")
            uploaded += 1
            remote = r2.head_size(item.relpath)
        poster = thumbs.path_for(item.relpath)
        if not poster.is_file():
            poster_ready = thumbs.ensure(item.relpath, local, wait=90.0)
            poster = poster_ready if poster_ready else poster
        if poster.is_file() and r2.head_size(thumbs.r2_key(item.relpath)) is None:
            r2.upload_file(poster, thumbs.r2_key(item.relpath), "image/jpeg")
        if not args.keep_local and remote == local.stat().st_size:
            local.unlink()
            deleted += 1
            print(f"  [x] 已删本地 {item.filename}")
    print(f"完成：上传 {uploaded}，已在桶内跳过 {skipped}，删除本地 {deleted}")


if __name__ == "__main__":
    main()
