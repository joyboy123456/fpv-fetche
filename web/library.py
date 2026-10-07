"""扫描 download_root 下的视频文件（分类/日期/文件）。"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".m4v", ".mov"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
RES_IN_NAME = re.compile(r"_(\d{3,4})p$", re.I)
MIME = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
}


@dataclass(frozen=True)
class VideoItem:
    relpath: str
    category: str
    date: str
    title: str
    filename: str
    size: int
    mtime: float
    resolution: str

    def as_dict(self) -> dict:
        return {
            "relpath": self.relpath,
            "category": self.category,
            "date": self.date,
            "title": self.title,
            "filename": self.filename,
            "size": self.size,
            "mtime": int(self.mtime),
            "resolution": self.resolution,
        }


def _title_from_stem(stem: str) -> tuple[str, str]:
    m = RES_IN_NAME.search(stem)
    if not m:
        return stem, ""
    return stem[: m.start()], m.group(1)


def item_from_relpath(rel: str, size: int, mtime: float) -> VideoItem | None:
    name = rel.rsplit("/", 1)[-1]
    if not name or name.startswith(".") or name.endswith(".part"):
        return None
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix not in VIDEO_EXTS:
        return None
    category, date = _split_rel(rel)
    title, res = _title_from_stem(Path(name).stem)
    return VideoItem(
        relpath=rel,
        category=category,
        date=date,
        title=title or Path(name).stem,
        filename=name,
        size=size,
        mtime=mtime,
        resolution=res,
    )


def _split_rel(rel: str) -> tuple[str, str]:
    parts = rel.split("/")
    if len(parts) == 1:
        return "未分类", ""
    category = parts[0]
    date = parts[1] if len(parts) >= 3 and DATE_RE.match(parts[1]) else ""
    return category, date


def scan(root: Path) -> list[VideoItem]:
    if not root.exists() or not root.is_dir():
        return []
    root = root.resolve()
    items: list[VideoItem] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith(".") or name.endswith(".part"):
                continue
            path = Path(dirpath) / name
            if path.suffix.lower() not in VIDEO_EXTS:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if st.st_size <= 0:
                continue
            rel = path.relative_to(root).as_posix()
            category, date = _split_rel(rel)
            title, res = _title_from_stem(path.stem)
            items.append(
                VideoItem(
                    relpath=rel,
                    category=category,
                    date=date,
                    title=title or path.stem,
                    filename=name,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    resolution=res,
                )
            )
    items.sort(key=lambda v: (v.mtime, v.relpath), reverse=True)
    return items


class LibraryCache:
    def __init__(self, root: Path, ttl: float = 20.0, source=None):
        self.root = root
        self.ttl = ttl
        self._source = source
        self._items: list[VideoItem] = []
        self._at: float | None = None

    def items(self) -> list[VideoItem]:
        now = time.monotonic()
        if self._at is None or now - self._at >= self.ttl:
            self._items = self._source() if self._source else scan(self.root)
            self._at = now
        return self._items

    def invalidate(self) -> None:
        self._at = None


def safe_file(root: Path, rel: str) -> Path | None:
    """把相对路径解析到 root 内的真实文件；越界或非视频返回 None。"""
    if not rel or len(rel) > 2048:
        return None
    rel = rel.replace("\\", "/").lstrip("/")
    candidate = Path(rel)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    root = root.resolve()
    try:
        target = (root / candidate).resolve()
    except (OSError, RuntimeError):
        return None
    if not target.is_relative_to(root):
        return None
    if not target.is_file():
        return None
    if target.suffix.lower() not in VIDEO_EXTS:
        return None
    return target


def mime_for(path: Path) -> str:
    return MIME.get(path.suffix.lower(), "application/octet-stream")
