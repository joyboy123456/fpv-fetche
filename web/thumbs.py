"""从视频抽一帧当封面。串行 ffmpeg，结果落到 download_root/.thumbs。"""
from __future__ import annotations

import hashlib
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

from web.library import safe_file

FFMPEG = shutil.which("ffmpeg")


class ThumbStore:
    def __init__(self, root: Path):
        self.root = root
        self.cache = root / ".thumbs"
        self.cache.mkdir(parents=True, exist_ok=True)
        self._q: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._queued: set[str] = set()

    @staticmethod
    def digest_for(rel: str) -> str:
        return hashlib.sha256(rel.encode("utf-8")).hexdigest()[:20]

    def path_for(self, rel: str) -> Path:
        return self.cache / f"{self.digest_for(rel)}.jpg"

    def r2_key(self, rel: str) -> str:
        return f".thumbs/{self.digest_for(rel)}.jpg"

    def ready(self, rel: str, video: Path) -> Path | None:
        out = self.path_for(rel)
        try:
            if out.is_file() and out.stat().st_size > 800 and out.stat().st_mtime >= video.stat().st_mtime:
                return out
        except OSError:
            return None
        return None

    def _failed(self, rel: str, video: Path) -> bool:
        mark = self.path_for(rel).with_suffix(".fail")
        try:
            return mark.is_file() and mark.stat().st_mtime >= video.stat().st_mtime
        except OSError:
            return False

    def enqueue(self, rel: str) -> None:
        if not FFMPEG:
            return
        with self._lock:
            if rel in self._queued:
                return
            self._queued.add(rel)
            self._q.put(rel)

    def ensure(self, rel: str, video: Path, wait: float = 0.0) -> Path | None:
        got = self.ready(rel, video)
        if got:
            return got
        if not FFMPEG or self._failed(rel, video):
            return None
        self.enqueue(rel)
        if wait <= 0:
            return None
        deadline = time.monotonic() + wait
        with self._cv:
            while time.monotonic() < deadline:
                got = self.ready(rel, video)
                if got or self._failed(rel, video):
                    return got
                self._cv.wait(timeout=min(1.0, deadline - time.monotonic()))
        return self.ready(rel, video)

    def _build(self, rel: str) -> None:
        video = safe_file(self.root, rel)
        if video is None or not FFMPEG:
            return
        if self.ready(rel, video) or self._failed(rel, video):
            return
        out = self.path_for(rel)
        tmp = out.with_name(out.stem + ".tmp.jpg")
        cmd = [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-threads",
            "1",
            "-i",
            str(video),
            "-ss",
            "8",
            "-frames:v",
            "1",
            "-an",
            "-vf",
            "scale=480:-2",
            "-q:v",
            "5",
            "-f",
            "image2",
            "-y",
            str(tmp),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=45)
            if tmp.is_file() and tmp.stat().st_size > 800:
                tmp.replace(out)
                return
        except (subprocess.SubprocessError, OSError):
            pass
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        out.with_suffix(".fail").write_bytes(b"")

    def run_worker(self) -> None:
        while True:
            rel = self._q.get()
            try:
                self._build(rel)
            finally:
                with self._cv:
                    self._queued.discard(rel)
                    self._cv.notify_all()
                self._q.task_done()
