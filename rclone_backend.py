#!/usr/bin/env python3
"""
fpv-fetcher · rclone 存储后端
================================
与 uploader.py 的 PCloudUploader 接口完全对齐（upload / list_folder /
get_filelink / close），供 fetcher.py、player.py 无差别调用。

适用场景：pCloud 自建 App 还在审核、拿不到 access_token。rclone 自带
官方已注册的 pCloud 接入凭证，`rclone config` 浏览器点一下授权即可用。

- 上传：`rclone copyto`（自带重试/断点，流式不占内存）
- 列表：`rclone lsjson`
- 播放源流：首次需要时拉起子进程 `rclone serve http`（只绑 127.0.0.1，
  天然支持 HTTP Range），player.py 中继时与普通 HTTP 源无差别

前置：系统已安装 rclone，且 `rclone config` 配好名为 rclone_remote 的
pcloud remote（配置文件默认 ~/.config/rclone/rclone.conf，
可用环境变量 RCLONE_CONFIG 指到别处）。
"""
from __future__ import annotations

import json
import posixpath
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import quote


class RcloneError(RuntimeError):
    """rclone 子进程返回非 0 或批量操作时抛出。"""


class RcloneBackend:
    def __init__(
        self,
        remote: str = "pcloud",
        base_folder: str = "/fpv-videos",
        serve_addr: str = "127.0.0.1:18787",
    ):
        self.remote = remote
        self.base = base_folder.strip("/")
        self._serve_addr = serve_addr
        self._serve_proc: subprocess.Popen | None = None

    # ---------- 内部工具 ----------
    def _remote_path(self, rel: str = "") -> str:
        path = posixpath.join(self.base, rel) if rel else self.base
        return f"{self.remote}:{path}"

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["rclone", *args], capture_output=True, text=True, check=False
        )

    # ---------- 与 PCloudUploader 对齐的四个方法 ----------
    def upload(self, local: Path, remote_dir: str) -> str:
        """上传 local 到 base/remote_dir 下，返回云端相对路径。"""
        dst = f"{self._remote_path(remote_dir)}/{local.name}"
        r = self._run("copyto", str(local), dst)
        if r.returncode != 0:
            raise RcloneError(f"rclone copyto 失败: {r.stderr.strip()[-400:]}")
        return posixpath.join(self.base, remote_dir, local.name).lstrip("/")

    def list_folder(self, remote_dir: str = "") -> list[dict]:
        """JSON 目录列表用 lsjson（lsf 无 --json，格式随版本漂移）。"""
        r = self._run("lsjson", self._remote_path(remote_dir))
        if r.returncode != 0:
            raise RcloneError(r.stderr.strip()[-400:] or "rclone lsjson 失败")
        return [
            {
                "name": e["Name"],
                "is_folder": bool(e["IsDir"]),
                "size": int(e.get("Size") or 0),
            }
            for e in json.loads(r.stdout or "[]")
        ]

    def get_filelink(self, rel: str) -> str:
        """返回本地 rclone serve 的 URL（无过期问题，player 的缓存层照用无碍）。"""
        self._ensure_serve()
        return f"http://{self._serve_addr}/" + "/".join(
            quote(seg, safe="") for seg in rel.split("/")
        )

    def close(self) -> None:
        if self._serve_proc and self._serve_proc.poll() is None:
            self._serve_proc.terminate()
            try:
                self._serve_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._serve_proc.kill()

    # ---------- rclone serve http 子进程生命周期 ----------
    def _ensure_serve(self) -> None:
        if self._serve_proc and self._serve_proc.poll() is None:
            return
        self._serve_proc = subprocess.Popen(
            ["rclone", "serve", "http", self._remote_path(),
             "--addr", self._serve_addr, "--read-only"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        host, _, port = self._serve_addr.rpartition(":")
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with socket.create_connection((host, int(port)), timeout=1):
                    return
            except OSError:
                if self._serve_proc.poll() is not None:
                    raise RcloneError(
                        "rclone serve http 启动失败（remote 未配置？运行 rclone config）"
                    )
                time.sleep(0.2)
        raise RcloneError("rclone serve http 端口等待超时")
