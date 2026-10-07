#!/usr/bin/env python3
"""
fpv-fetcher · 在线播放站（VPS 中转模式）
==========================================
架构：浏览器 --中国线路--> 本站(VPS) --高速出口--> pCloud

- 存储后端由 config.json 的 pcloud.backend 决定：
    api    = pCloud 原生 API + CDN 临时直链（缓存 ~3h）
    rclone = 本地 `rclone serve http`（127.0.0.1，免审核）
- 目录列表经后端 list_folder（60s 内存缓存）
- 播放：后端取源 URL 后 VPS 中流式中继转发，
  Range 头浏览器<->源 双向透传——HTML5 播放器拖进度条依赖它
- 大流量全走 VPS 中转，浏览器不接触 pCloud，天然绕开国内访问慢的问题

运行：
  FPV_WEB_USER=me FPV_WEB_PASS=强密码 PORT=8080 python3 player.py
认证：设置 FPV_WEB_USER/FPV_WEB_PASS 后启用 HTTP Basic Auth；不设则无鉴权（不要公网裸奔）。
"""
from __future__ import annotations

import os
import posixpath
import sys
import time
from urllib.parse import quote

import httpx
from flask import Flask, Response, abort, render_template, request, stream_with_context

from fetcher import build_uploader, load_config
from rclone_backend import RcloneError
from uploader import PCloudError

LINK_TTL = 3 * 3600 - 300  # CDN 直链缓存时长（提前 5 分钟刷新，实际有效期以 pCloud 为准）
LIST_TTL = 60.0            # 目录列表缓存
CHUNK = 1 << 16            # 中继转发粒度 64 KiB

app = Flask(__name__)

_cfg_path = os.environ.get("FPV_CONFIG", "config.json")
cfg = load_config(_cfg_path)
_up_err = None
try:
    up = build_uploader(cfg.pcloud) if cfg.pcloud.enabled else None
    if up is None:
        _up_err = "config.json 里 pcloud.enabled=false"
except Exception as e:
    up = None
    _up_err = str(e)

# 连接 pCloud CDN 的客户端：读超时无限（长视频流），连接超时兜底
cdn = httpx.Client(
    follow_redirects=True,
    timeout=httpx.Timeout(None, connect=30.0),
)

_link_cache: dict[str, tuple[float, str]] = {}
_list_cache: dict[str, tuple[float, list[dict]]] = {}

AUTH_USER = os.environ.get("FPV_WEB_USER")
AUTH_PASS = os.environ.get("FPV_WEB_PASS")


@app.before_request
def _auth():
    if not AUTH_USER:
        return None
    a = request.authorization
    if not a or a.username != AUTH_USER or a.password != AUTH_PASS:
        return Response("Auth required", 401,
                        {"WWW-Authenticate": 'Basic realm="fpv"'})


def _safe(rel: str) -> str:
    """防路径穿越。"""
    rel = rel.strip("/")
    if any(seg in ("", ".", "..") for seg in rel.split("/")) and rel:
        abort(404)
    return rel


def _list(rel: str) -> list[dict]:
    if up is None:
        abort(503, description=f"pCloud 后端未就绪: {_up_err}")
    ent = _list_cache.get(rel)
    if ent and time.time() - ent[0] < LIST_TTL:
        return ent[1]
    try:
        items = up.list_folder(rel)
    except (PCloudError, RcloneError):
        abort(404)
    items.sort(key=lambda x: (not x["is_folder"], x["name"]), reverse=False)
    _list_cache[rel] = (time.time(), items)
    return items


def _cdn_link(rel: str) -> str:
    if up is None:
        abort(503, description=f"pCloud 后端未就绪: {_up_err}")
    ent = _link_cache.get(rel)
    if ent and time.time() - ent[0] < LINK_TTL:
        return ent[1]
    try:
        url = up.get_filelink(rel)
    except (PCloudError, RcloneError):
        abort(404)
    _link_cache[rel] = (time.time(), url)
    return url


PAGE_NOTE = "前端模板在 templates/ 目录（browse.html / watch.html），改 UI 直接编辑即可，Jinja 自动转义防 XSS"


@app.route("/")
def index():
    return _browse("")


@app.route("/browse/<path:rel>")
def browse(rel: str):
    return _browse(_safe(rel))


def _browse(rel: str):
    entries = []
    for it in _list(rel):
        is_video = it["name"].lower().endswith((".mp4", ".mkv", ".webm", ".mov"))
        if not (it["is_folder"] or is_video):
            continue
        entries.append({
            "name": it["name"],
            "is_folder": it["is_folder"],
            "url": quote(posixpath.join(rel, it["name"])),
            "size_mb": f'{it["size"] / 1048576:.0f}',
        })
    return render_template(
        "browse.html", rel=rel, items=entries,
        back=quote(posixpath.dirname(rel)) if rel else "",
    )


@app.route("/watch/<path:rel>")
def watch(rel: str):
    rel = _safe(rel)
    return render_template(
        "watch.html",
        name=posixpath.basename(rel),
        rel=quote(rel),
        back=quote(posixpath.dirname(rel)),
    )


@app.route("/stream/<path:rel>")
def stream(rel: str):
    """Range 透传中继：把浏览器的 Range 原样发给 CDN，把 CDN 的 206/200 原样回给浏览器。"""
    rel = _safe(rel)
    url = _cdn_link(rel)
    fwd = {"Range": request.headers["Range"]} if "Range" in request.headers else {}
    r = cdn.send(cdn.build_request("GET", url, headers=fwd), stream=True)
    if r.status_code not in (200, 206):
        r.close()
        _link_cache.pop(rel, None)  # 直链可能过期，清缓存下次重新取
        abort(502)
    headers = {}
    for h in ("Content-Length", "Content-Range", "Content-Type"):
        if h in r.headers:
            headers[h] = r.headers[h]
    headers["Accept-Ranges"] = "bytes"

    def gen():
        try:
            for chunk in r.iter_bytes(CHUNK):
                yield chunk
        finally:
            r.close()

    return Response(stream_with_context(gen()), status=r.status_code, headers=headers)


if __name__ == "__main__":
    if not AUTH_USER:
        print("[!] 未设置 FPV_WEB_USER/FPV_WEB_PASS，站点无鉴权，勿暴露公网", file=sys.stderr)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), threaded=True)
