#!/usr/bin/env python3
"""
fpv-fetcher · pCloud 云存储上传模块
====================================
直接调用 pCloud HTTP API（无第三方依赖，复用项目已有的 httpx）：
  createfolderifnotexists → 确保远端目录存在（按段缓存 folderid）
  uploadfile             → multipart 流式上传，本地文件不会整块读进内存

认证：OAuth2 access_token（在 https://docs.pcloud.com/ 注册 App 后获取）。
区服：region="us" → api.pcloud.com；region="eu" → eapi.pcloud.com（注册地是欧洲FC必选）。
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path

import httpx

API_HOSTS = {
    "us": "https://api.pcloud.com",
    "eu": "https://eapi.pcloud.com",
}


@dataclass
class PCloudCfg:
    enabled: bool = False
    access_token: str = ""
    region: str = "us"  # us | eu
    base_folder: str = "/fpv-videos"  # 云端根目录，其下结构 = 分类/日期/视频
    delete_local_after_upload: bool = False  # True=本地只做中转，传完即删
    timeout: float = 30.0  # 控制类请求超时（上传本体不设超时，文件名 GB 级也安全）
    backend: str = "api"  # api=pCloud 原生 HTTP API(需自建 App token)；rclone=本地 rclone 进程(免审核)
    rclone_remote: str = "pcloud"  # backend=rclone 时，`rclone config` 里配置的 remote 名


class PCloudError(RuntimeError):
    """pCloud API 返回非 0 result 时抛出。"""


def _check(r: httpx.Response) -> dict:
    data = r.json()
    if data.get("result") != 0:
        raise PCloudError(f"pCloud 错误 {data.get('result')}: {data.get('error')}")
    return data


class PCloudUploader:
    """一条长连接复用；folderid 缓存避免重复建目录。"""

    def __init__(self, cfg: PCloudCfg):
        host = API_HOSTS.get(cfg.region, API_HOSTS["us"])
        self.client = httpx.Client(
            base_url=host,
            params={"access_token": cfg.access_token},
            timeout=cfg.timeout,
        )
        self.base = "/" + cfg.base_folder.strip("/")
        self._folder_cache: dict[str, int] = {}

    def ensure_folder(self, remote_dir: str = "") -> int:
        """确保 base/remote_dir 逐级存在，返回最里层 folderid。"""
        path = self.base if not remote_dir else posixpath.join(self.base, remote_dir)
        if path in self._folder_cache:
            return self._folder_cache[path]
        fid = 0  # pCloud 根目录永远是 0
        for seg in path.strip("/").split("/"):
            data = _check(
                self.client.post(
                    "/createfolderifnotexists",
                    params={"folderid": fid, "name": seg},
                )
            )
            fid = data["metadata"]["folderid"]
        self._folder_cache[path] = fid
        return fid

    def upload(self, local: Path, remote_dir: str) -> str:
        """上传 local 到 remote_dir（base 之下的相对目录），返回云端路径。"""
        fid = self.ensure_folder(remote_dir)
        with local.open("rb") as f:
            r = self.client.post(
                "/uploadfile",
                params={
                    "folderid": fid,
                    "filename": local.name,
                    "renameifexists": 1,  # 重名自动改名，不覆盖不报错
                },
                files={"file": (local.name, f, "application/octet-stream")},
                timeout=None,  # 大文件不设限，交给外层调度节奏
            )
        _check(r)
        return posixpath.join(self.base, remote_dir, local.name).lstrip("/")

    # ---------- 以下两个方法供在线播放站（site.py）使用 ----------
    def list_folder(self, remote_dir: str = "") -> list[dict]:
        """列出 base/remote_dir 下的子目录和文件。"""
        path = self.base if not remote_dir else posixpath.join(self.base, remote_dir)
        data = _check(self.client.get("/listfolder", params={"path": path}))
        out = []
        for m in data["metadata"].get("contents", []):
            out.append({
                "name": m["name"],
                "is_folder": bool(m.get("isfolder")),
                "size": int(m.get("size") or 0),
            })
        return out

    def get_filelink(self, rel: str) -> str:
        """取文件的 pCloud CDN 临时直链（有效期数小时，调用方应缓存）。"""
        path = posixpath.join(self.base, rel)
        data = _check(self.client.get("/getfilelink", params={"path": path}))
        return "https://" + data["hosts"][0] + data["path"]

    def close(self) -> None:
        self.client.close()
