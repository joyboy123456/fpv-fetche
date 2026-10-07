#!/usr/bin/env python3
"""
fpv-fetcher · 核心抓取模块
================================
从油猴脚本逻辑移植：视频页 → get_file → fpvcdn → ahcdn 直链 → 流式下载。

服务器端出口 IP 固定，签名绑定的 IP 与下载 IP 天然一致，
因此油猴脚本里的「IP 轮换重试」逻辑在此完全不需要（KISS）。

目录结构：download_root / 分类名 / YYYY-MM-DD / 标题_分辨率p.mp4
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from rclone_backend import RcloneBackend
from uploader import PCloudCfg, PCloudUploader


def build_uploader(pc: PCloudCfg):
    """按配置构造存储后端：api=pCloud 原生 HTTP API（需自建 App token）；
    rclone=本地 rclone 进程（免审核，rclone 自带官方接入凭证）。
    两者接口一致，fetcher/player 无差别调用。player.py 也复用本函数。"""
    if pc.backend == "rclone":
        return RcloneBackend(pc.rclone_remote, pc.base_folder)
    if not pc.access_token:
        raise RuntimeError("pcloud.backend=api 但 access_token 为空（或改用 backend=rclone）")
    return PCloudUploader(pc)

# ==================== 模块级正则（可能需按站点实际结构调整）====================
# 视频页 <source src=".../get_file?...XXXm.mp4">
SOURCE_RE = re.compile(r"""<source[^>]+src=['"]([^'"]*get_file[^'"]*)['"]""", re.I)
# get_file URL 里的分辨率：1080m.mp4 / 720p.mp4
RES_RE = re.compile(r'(\d{3,4})[mp]\.mp4', re.I)
# 分类页里的视频链接（实测：HTML 中为绝对 URL，形如 https://host/videos/{数字ID}/{slug}/；兼容相对路径）
VIDEO_LINK_RE = re.compile(r'href="((?:https?://[^\s"/]+)?/videos/\d+/[^"#?]+)"', re.I)
# 视频页标题（og:title 优先）
TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', re.I)


# ==================== 配置数据类 ====================
@dataclass
class CategoryCfg:
    name: str
    url: str
    pages: int = 1  # 每次抓取扫描的分类页数（1=只看首页最新）


@dataclass
class Config:
    download_root: Path
    categories: list[CategoryCfg]
    resolution: str = "highest"
    cookie: str = ""
    proxy: str | None = None  # 可选：http://user:pass@host:port（出口 IP 被 Cloudflare 拦时填）
    max_per_run: int = 0  # 每个分类单次运行最多下载几个，0=不限制（磁盘保护）
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    history_file: Path = None  # type: ignore
    uploaded_file: Path = None  # type: ignore  # 已上传 pCloud 的本地相对路径清单
    pcloud: PCloudCfg = field(default_factory=PCloudCfg)
    request_timeout: float = 30.0
    download_timeout: float = 600.0
    max_retries: int = 3


def load_config(path: str | Path) -> Config:
    """从 JSON 加载配置；本函数是配置构造的唯一出口（DRY）。"""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    cats = [CategoryCfg(**c) for c in raw["categories"]]
    root = Path(raw["download_root"])
    hist = raw.get("history_file") or str(root / ".history.json")
    uploaded = raw.get("uploaded_file") or str(root / ".uploaded.json")
    return Config(
        download_root=root,
        categories=cats,
        resolution=raw.get("resolution", "highest"),
        cookie=raw.get("cookie", ""),
        proxy=raw.get("proxy") or None,
        user_agent=raw.get("user_agent") or Config.user_agent,
        history_file=Path(hist),
        uploaded_file=Path(uploaded),
        pcloud=PCloudCfg(**{k: v for k, v in raw.get("pcloud", {}).items()
                            if k in PCloudCfg.__dataclass_fields__}),
        request_timeout=raw.get("request_timeout", 30.0),
        download_timeout=raw.get("download_timeout", 600.0),
        max_retries=raw.get("max_retries", 3),
    )


# ==================== 历史记录：单一职责，避免重复抓取 ====================
class History:
    """已下载视频 URL 集合，持久化到 JSON。"""

    def __init__(self, path: Path):
        self.path = path
        self.data: set[str] = set()
        if path.exists():
            try:
                self.data = set(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                self.data = set()

    def has(self, url: str) -> bool:
        return url in self.data

    def add(self, url: str) -> None:
        self.data.add(url)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(sorted(self.data), ensure_ascii=False, indent=0),
            encoding="utf-8",
        )


# ==================== 核心抓取器 ====================
class Fetcher:
    """单实例贯穿一次抓取流程，复用 HTTP 连接（DRY：请求工具集中管理）。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.history = History(cfg.history_file)
        self.uploaded = History(cfg.uploaded_file)
        self.uploader = build_uploader(cfg.pcloud) if cfg.pcloud.enabled else None
        headers = {
            "User-Agent": cfg.user_agent,
            "Accept-Language": "en-US,en;q=0.9",
        }
        if cfg.cookie:
            headers["Cookie"] = cfg.cookie
        # 关键：不自动跟随重定向——我们要手动逐跳捕获 Location（与油猴逻辑一致）
        self.client = httpx.Client(
            headers=headers,
            follow_redirects=False,
            timeout=cfg.request_timeout,
            proxy=cfg.proxy or None,
        )

    # ---------- 工具：手动跟随一跳重定向 ----------
    def _manual_redirect(self, url: str, referer: str | None = None) -> str | None:
        """
        请求 url（流式，只读响应头，立即丢弃响应体——
        fpvcdn 可能直接 200 返回整个视频体，普通 get() 会把整片缓冲进内存）：
        - 3xx → 返回 Location 绝对地址
        - 200/206 → url 本身即直链，返回 url
        - 403 → IP 不匹配，重试（服务器端极少发生，留作兜底）
        其余失败返回 None
        """
        headers = {"Referer": referer} if referer else {}
        for _ in range(self.cfg.max_retries):
            try:
                with self.client.stream("GET", url, headers=headers) as r:
                    # 退出 with 即丢弃未读取的响应体，连接随之释放
                    if r.status_code in (301, 302, 303, 307, 308):
                        loc = r.headers.get("location")
                        return urljoin(url, loc) if loc else None
                    if r.status_code in (200, 206):
                        return url
                    if r.status_code == 403:
                        time.sleep(1.5)
                        continue
                    return None
            except httpx.HTTPError:
                return None
        return None

    # ---------- 步骤 1：分类页 → 视频页 URL 列表 ----------
    def list_videos(self, cat: CategoryCfg) -> list[str]:
        """分类页分页实测为 {url}{N}/ 形式，如 /categories/solo/2/"""
        base = cat.url if cat.url.endswith("/") else cat.url + "/"
        seen: set[str] = set()
        urls: list[str] = []
        for page in range(1, max(1, cat.pages) + 1):
            page_url = base if page == 1 else f"{base}{page}/"
            try:
                r = self.client.get(page_url)
            except httpx.HTTPError as e:
                print(f"  [!] 分类页请求失败 {page_url}: {e}", file=sys.stderr)
                continue
            if r.status_code != 200:
                print(f"  [!] 分类页 {page_url}: HTTP {r.status_code}", file=sys.stderr)
                continue
            for m in VIDEO_LINK_RE.finditer(r.text):
                link = urljoin(page_url, m.group(1))
                if link not in seen:
                    seen.add(link)
                    urls.append(link)
        return urls

    # ---------- 步骤 2：视频页 → (标题, 按分辨率排序的 get_file 源) ----------
    def parse_video_page(self, video_url: str) -> tuple[str, list[tuple[str, str]]]:
        r = self.client.get(video_url)
        if r.status_code != 200:
            raise RuntimeError(f"视频页请求失败: HTTP {r.status_code}")
        title_m = TITLE_RE.search(r.text)
        fallback = urlparse(video_url).path.strip("/").split("/")[-1]
        title = title_m.group(1) if title_m else (fallback or "video")
        # 同分辨率去重
        by_res: dict[str, str] = {}
        for m in SOURCE_RE.finditer(r.text):
            src = urljoin(video_url, m.group(1))
            rm = RES_RE.search(src)
            res = rm.group(1) if rm else "0"
            by_res.setdefault(res, src)
        ranked = sorted(by_res.items(), key=lambda x: int(x[0]), reverse=True)
        return title, ranked

    # ---------- 步骤 3：get_file → fpvcdn → ahcdn 直链 ----------
    def resolve_final_url(self, get_file_url: str, referer: str) -> str:
        # get_file 同源 302 → fpvcdn
        fpv = self._manual_redirect(get_file_url, referer=referer)
        if not fpv:
            raise RuntimeError("get_file 未返回跳转")
        # fpvcdn → ahcdn 或直接 200 直链
        final = self._manual_redirect(fpv, referer=referer)
        if not final:
            raise RuntimeError("fpvcdn 校验失败（403 IP 不匹配？）")
        return final

    # ---------- 步骤 4：流式下载到 分类/日期/文件 ----------
    def download(
        self, final_url: str, referer: str, cat_name: str, title: str, res: str
    ) -> Path:
        safe_title = self._sanitize(title)
        date_dir = self.cfg.download_root / cat_name / datetime.now().strftime("%Y-%m-%d")
        date_dir.mkdir(parents=True, exist_ok=True)
        out = date_dir / f"{safe_title}_{res}p.mp4"
        if out.exists() and out.stat().st_size > 0:
            return out  # 断点/重复保护
        tmp = out.with_suffix(".mp4.part")
        headers = {"Referer": referer, "Range": "bytes=0-"}
        with self.client.stream(
            "GET", final_url, headers=headers, timeout=self.cfg.download_timeout
        ) as r:
            if r.status_code not in (200, 206):
                raise RuntimeError(f"下载失败: HTTP {r.status_code}")
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(chunk_size=1 << 20):  # 1 MiB
                    f.write(chunk)
        tmp.replace(out)
        return out

    @staticmethod
    def _sanitize(name: str) -> str:
        name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip().strip(".")
        return name[:120] or "video"

    # ---------- 步骤 5（可选）：上传 pCloud ----------
    def _upload_one(self, local: Path, rel: str) -> bool:
        """上传单个本地文件（rel=download_root 下相对路径）。失败留在本地等下轮补传。"""
        if not self.uploader or self.uploaded.has(rel):
            return False
        size_mb = local.stat().st_size / 1048576
        print(f"  [⇑] 上传 pCloud: {rel} ({size_mb:.1f} MiB)")
        remote = self.uploader.upload(local, posixpath.dirname(rel))
        self.uploaded.add(rel)
        print(f"  [☁ ] 云端就绪: /{remote}")
        if self.cfg.pcloud.delete_local_after_upload:
            local.unlink(missing_ok=True)
            d = local.parent
            while d != self.cfg.download_root:
                try:
                    os.rmdir(d)  # 只有空目录才删得掉，非空直接报错跳过
                except OSError:
                    break
                d = d.parent
            print(f"  [C] 本地中转副本已删除")
        return True

    def upload_pending(self) -> int:
        """扫描 download_root，把本地还在但云上没有的 mp4 全部补传到 pCloud。"""
        if not self.uploader:
            return 0
        done = 0
        for f in sorted(self.cfg.download_root.rglob("*.mp4")):
            rel = f.relative_to(self.cfg.download_root).as_posix()
            try:
                done += self._upload_one(f, rel)
            except Exception as e:
                print(f"  [!] 上传失败 {rel}: {e}", file=sys.stderr)
        return done

    # ---------- 主流程：处理一个分类 ----------
    def run_category(self, cat: CategoryCfg) -> int:
        print(f"[{cat.name}] 列出视频…")
        videos = self.list_videos(cat)
        print(f"[{cat.name}] 发现 {len(videos)} 个视频")
        downloaded = 0
        for url in videos:
            if self.history.has(url):
                continue
            try:
                title, sources = self.parse_video_page(url)
                if not sources:
                    print(f"  [-] 未发现源: {url}")
                    self.history.add(url)  # 标记已处理，避免反复尝试
                    continue
                if self.cfg.resolution == "highest":
                    res, get_file = sources[0]
                else:
                    res, get_file = next(
                        (x for x in sources if x[0] == self.cfg.resolution), sources[0]
                    )
                print(f"  [↓] {title} [{res}p]")
                final = self.resolve_final_url(get_file, referer=url)
                out = self.download(
                    final, referer=url, cat_name=cat.name, title=title, res=res
                )
                size_mb = out.stat().st_size / 1048576
                print(f"  [✅] {out.name} ({size_mb:.1f} MiB)")
                self.history.add(url)
                downloaded += 1
                try:
                    self._upload_one(
                        out, out.relative_to(self.cfg.download_root).as_posix()
                    )
                except Exception as e:
                    print(f"  [!] 上传失败，文件留在本地下轮补传: {e}", file=sys.stderr)
            except Exception as e:
                print(f"  [!] 失败 {url}: {e}", file=sys.stderr)
        return downloaded

    def close(self) -> None:
        self.client.close()
        if self.uploader:
            self.uploader.close()
