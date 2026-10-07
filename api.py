#!/usr/bin/env python3
"""
fpv-fetcher · HTTP API 服务
============================
把抓取流程封装成 HTTP 接口，供其他服务器远程调用（异步任务模型：
提交任务立即返回 job_id，下载在后台进行，用 job_id 轮询结果）。

接口一览（均为 JSON）：
  POST /api/fetch        提交抓取任务
                         body: {"mode": "urls"|"category"|"all",
                                "urls": [...],            # mode=urls 时必填：视频页地址
                                "category": "amateur",    # mode=category 时：config.json 里的分类名
                                                        # mode=urls 时：可选，文件归到的子目录，默认 "api"
                                "resolution": "720"}      # 可选，覆盖 config.json 的分辨率
  GET  /api/job/<id>     查询任务状态与结果
  GET  /api/jobs         列出所有任务
  GET  /api/list/<name>  只列出分类下的视频页 URL，不下载（?pages=2 覆盖页数）
  POST /api/resolve      只解析视频页 → 标题 + 各分辨率 get_file 源 + 最终直链，不下载

鉴权：设置 FPV_API_TOKEN 后，请求需带 Authorization: Bearer <token> 或 X-API-Token；
不设则无鉴权（不要公网裸奔）。
运行：FPV_API_TOKEN=xxx PORT=9090 python3 api.py

说明：
- 任务按提交顺序串行执行（全局锁），避免并发下载抢带宽 + 历史文件写坏
- 历史记录 (.history.json) 与命令行模式共用，同一 URL 不会被重复下载
"""
from __future__ import annotations

import os
import secrets
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from urllib.parse import quote

from flask import Flask, Response, jsonify, request

from fetcher import Fetcher, load_config

app = Flask(__name__)

_cfg_path = os.environ.get("FPV_CONFIG", "config.json")
_cfg = load_config(_cfg_path)

AUTH_TOKEN = os.environ.get("FPV_API_TOKEN", "")

# 全局锁：同一时刻只跑一个抓取任务
_job_lock = threading.Lock()
_jobs: dict[str, "Job"] = {}
_jobs_mu = threading.Lock()


@dataclass
class Job:
    id: str
    mode: str
    params: dict
    state: str = "queued"  # queued / running / done / error
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    downloaded: list[dict] = field(default_factory=list)
    skipped: int = 0       # 历史记录里已有而跳过的数量
    failed: list[dict] = field(default_factory=list)
    error: str | None = None

    def public(self) -> dict:
        d = {
            "id": self.id,
            "mode": self.mode,
            "params": self.params,
            "state": self.state,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "downloaded": self.downloaded,
            "skipped": self.skipped,
            "failed": self.failed,
            "error": self.error,
        }
        if self.state in ("done", "error"):
            d["duration_sec"] = round((self.finished or 0) - (self.started or 0), 1)
        return d


def _auth():
    if not AUTH_TOKEN:
        return None
    a = request.headers.get("Authorization", "")
    if a == f"Bearer {AUTH_TOKEN}":
        return None
    if request.headers.get("X-API-Token") == AUTH_TOKEN:
        return None
    return jsonify(error="unauthorized"), 401


def _cat_name(cat: str) -> str:
    """分类名白名单校验：只允许出现在 config.json 里的分类，防乱建目录。"""
    for c in _cfg.categories:
        if c.name == cat:
            return c
    raise LookupError(f"未知分类: {cat}（可用: {', '.join(c.name for c in _cfg.categories)}）")


def _process_url(f: Fetcher, job: Job, url: str, cat_name: str) -> None:
    """处理单个视频页：解析 → 直链 → 下载 → 上传。结果写入 job。"""
    if f.history.has(url):
        job.skipped += 1
        return
    title, sources = f.parse_video_page(url)
    if not sources:
        f.history.add(url)  # 无源也标记，避免反复尝试（与 run_category 一致）
        job.failed.append({"url": url, "error": "页面未发现视频源"})
        return
    if f.cfg.resolution == "highest":
        res, get_file = sources[0]
    else:
        res, get_file = next(
            (x for x in sources if x[0] == f.cfg.resolution), sources[0]
        )
    final = f.resolve_final_url(get_file, referer=url)
    out = f.download(final, referer=url, cat_name=cat_name, title=title, res=res)
    size_mb = out.stat().st_size / 1048576
    item = {
        "url": url,
        "title": title,
        "resolution": res,
        "file": str(out),
        "relative_path": out.relative_to(f.cfg.download_root).as_posix(),
        "size_mb": round(size_mb, 1),
        "remote_path": None,
    }
    f.history.add(url)
    # 可选：上传云存储（复用 fetcher 的统一入口，失败留本地等下轮补传）
    if f.uploader:
        try:
            f._upload_one(out, item["relative_path"])
            if f.cfg.pcloud.delete_local_after_upload and not out.exists():
                item["remote_path"] = f"{f.cfg.pcloud.base_folder}/{quote(item['relative_path'])}"
                item["file"] = None  # 本地已删
        except Exception as e:
            job.failed.append({"url": url, "error": f"上传失败（文件已留在本地）: {e}"})
    job.downloaded.append(item)


def _run_job(job: Job) -> None:
    job.state = "running"
    job.started = time.time()
    try:
        # 分辨率可在请求里覆盖（只影响本次任务）
        cfg = replace(_cfg, resolution=job.params["resolution"]) \
            if job.params.get("resolution") else _cfg
        f = Fetcher(cfg)
        try:
            p = job.params
            if job.mode == "urls":
                for url in p["urls"]:
                    _process_url(f, job, url, p.get("category", "api"))
            else:
                cats = _cfg.categories if job.mode == "all" else [_cat_name(p["category"])]
                limit = p.get("max_per_run", 0)
                for cat in cats:
                    for url in f.list_videos(cat):
                        if job.mode == "category" and limit and \
                           len(job.downloaded) >= limit:
                            break
                        _process_url(f, job, url, cat.name)
        finally:
            f.close()
        job.state = "done"
    except Exception as e:
        job.state = "error"
        job.error = str(e)
        print(f"[!] 任务 {job.id} 异常: {e}", file=sys.stderr)
    finally:
        job.finished = time.time()


def _submit(mode: str, params: dict):
    job = Job(id=uuid.uuid4().hex[:12], mode=mode, params=params)
    with _jobs_mu:
        _jobs[job.id] = job

    def worker():
        with _job_lock:
            _run_job(job)

    threading.Thread(target=worker, daemon=True, name=f"job-{job.id}").start()
    return jsonify(id=job.id, state=job.state), 202


@app.before_request
def _auth_hook():
    return _auth()


@app.post("/api/fetch")
def fetch():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode", "all")
    if mode not in ("urls", "category", "all"):
        return jsonify(error="mode 必须是 urls / category / all"), 400
    params = {}
    if mode == "urls":
        urls = body.get("urls")
        if not urls or not isinstance(urls, list):
            return jsonify(error="mode=urls 需要提供 urls 数组（视频页地址）"), 400
        params["urls"] = urls
        if body.get("category"):
            params["category"] = str(body["category"])
    elif mode == "category":
        try:
            _cat_name(body.get("category", ""))
        except LookupError as e:
            return jsonify(error=str(e)), 400
        params["category"] = body["category"]
        params["max_per_run"] = int(body.get("max_per_run", 0))
    if body.get("resolution"):
        params["resolution"] = str(body["resolution"])
    return _submit(mode, params)


@app.get("/api/job/<job_id>")
def job_status(job_id: str):
    with _jobs_mu:
        job = _jobs.get(job_id)
    if not job:
        return jsonify(error="任务不存在"), 404
    return jsonify(job.public())


@app.get("/api/jobs")
def job_list():
    with _jobs_mu:
        jobs = sorted(_jobs.values(), key=lambda j: j.created, reverse=True)
    return jsonify([j.public() for j in jobs])


@app.get("/api/list/<name>")
def list_videos(name: str):
    """只列出分类下的视频页 URL（按 config.json 的 pages 设置），不下载。"""
    try:
        cat = _cat_name(name)
    except LookupError as e:
        return jsonify(error=str(e)), 400
    pages = request.args.get("pages", type=int)
    c = replace(cat, pages=pages) if pages else cat
    f = Fetcher(_cfg)
    try:
        return jsonify(category=name, urls=f.list_videos(c))
    finally:
        f.close()


@app.post("/api/resolve")
def resolve():
    """解析视频页：返回标题、各分辨率 get_file 源、最终直链（不下载）。
    其他服务器可拿 final_url 自己下载（需带对应 Referer，有效期不确定，建议尽快用）。"""
    body = request.get_json(silent=True) or {}
    url = body.get("url")
    if not url:
        return jsonify(error="需要 url（视频页地址）"), 400
    resolution = str(body.get("resolution") or _cfg.resolution)
    f = Fetcher(_cfg)
    try:
        title, sources = f.parse_video_page(url)
        if not sources:
            return jsonify(error="页面未发现视频源"), 404
        res, get_file = next(
            (x for x in sources if x[0] == resolution), sources[0]
        )
        final = f.resolve_final_url(get_file, referer=url)
        return jsonify(
            title=title,
            resolution=res,
            final_url=final,
            referer=url,
            sources=[{"resolution": r, "get_file": g} for r, g in sources],
        )
    except Exception as e:
        return jsonify(error=str(e)), 502
    finally:
        f.close()


if __name__ == "__main__":
    if not AUTH_TOKEN:
        print("[!] 未设置 FPV_API_TOKEN，API 无鉴权，勿暴露公网", file=sys.stderr)
    app.run(
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "9090")),
        threaded=True,
    )
