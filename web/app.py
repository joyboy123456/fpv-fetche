"""FastAPI 片库：列目录 + Range 直出 MP4，不转码。"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from fetcher import Config
from web.auth import COOKIE_NAME, LoginGate, issue_token, password_ok, token_ok
from web.library import LibraryCache, mime_for, safe_file
from web.r2 import R2Store
from web.thumbs import FFMPEG, ThumbStore

STATIC_DIR = Path(__file__).resolve().parent / "static"


class LoginBody(BaseModel):
    password: str = Field(min_length=1, max_length=256)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def create_app(cfg: Config) -> FastAPI:
    r2 = R2Store.from_config(cfg)
    lib = LibraryCache(
        cfg.download_root,
        source=(r2.list_videos if r2 else None),
    )
    thumbs = ThumbStore(cfg.download_root)
    gate = LoginGate()
    secret = cfg.web_secret or cfg.web_password or "fpv-web"
    need_auth = bool(cfg.web_password)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        worker = threading.Thread(target=thumbs.run_worker, name="thumb-worker", daemon=True)
        worker.start()
        if FFMPEG and r2 is None:
            for item in lib.items():
                thumbs.enqueue(item.relpath)
        yield

    app = FastAPI(title="fpv-web", docs_url=None, redoc_url=None, lifespan=lifespan)

    def check_auth(request: Request) -> None:
        if not need_auth:
            return
        if not token_ok(secret, request.cookies.get(COOKIE_NAME)):
            raise HTTPException(status_code=401, detail="需要口令")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        path = request.url.path
        if path.startswith("/static"):
            response.headers.setdefault("Cache-Control", "public, max-age=86400")
        elif path.startswith("/media"):
            response.headers.setdefault("Cache-Control", "private, max-age=3600")
        elif path.startswith("/thumb"):
            pass
        else:
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.get("/api/meta")
    def meta() -> dict:
        return {"auth_required": need_auth, "name": "REEL"}

    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response) -> dict:
        if not need_auth:
            return {"ok": True}
        ip = _client_ip(request)
        if gate.blocked(ip):
            raise HTTPException(status_code=429, detail="尝试过多，稍后再试")
        if not password_ok(body.password, cfg.web_password):
            gate.fail(ip)
            raise HTTPException(status_code=403, detail="口令不对")
        gate.ok(ip)
        secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
        response.set_cookie(
            COOKIE_NAME,
            issue_token(secret),
            httponly=True,
            samesite="lax",
            secure=secure,
            max_age=30 * 24 * 3600,
            path="/",
        )
        return {"ok": True}

    @app.post("/api/logout")
    def logout(response: Response) -> dict:
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/library")
    def library(request: Request, q: str = "", category: str = "") -> dict:
        check_auth(request)
        qn = q.strip().lower()
        items = []
        cats: dict[str, int] = {}
        for v in lib.items():
            cats[v.category] = cats.get(v.category, 0) + 1
            if category and v.category != category:
                continue
            if qn and qn not in v.title.lower() and qn not in v.relpath.lower():
                continue
            items.append(v.as_dict())
        return {
            "total": len(items),
            "categories": [
                {"name": name, "count": cats[name]}
                for name in sorted(cats, key=lambda n: (-cats[n], n))
            ],
            "items": items,
        }

    @app.api_route("/media", methods=["GET", "HEAD"])
    def media(request: Request, path: str = ""):
        check_auth(request)
        if r2 is not None and r2.exists(path):
            return RedirectResponse(r2.presign(path), status_code=302)
        target = safe_file(cfg.download_root, path)
        if target is None:
            raise HTTPException(status_code=404, detail="找不到片子")
        return FileResponse(
            target,
            media_type=mime_for(target),
            headers={"Accept-Ranges": "bytes", "Content-Disposition": "inline"},
        )

    @app.get("/thumb")
    def thumb(request: Request, path: str = ""):
        check_auth(request)
        key = thumbs.r2_key(path)
        if r2 is not None and r2.exists(key):
            return RedirectResponse(r2.presign(key), status_code=302)
        target = safe_file(cfg.download_root, path)
        if target is None:
            raise HTTPException(status_code=404, detail="找不到片子")
        poster = thumbs.ensure(path, target, wait=45.0)
        if poster is None:
            raise HTTPException(status_code=404, detail="还没有封面")
        if r2 is not None:
            r2.upload_file(poster, key, "image/jpeg")
            return RedirectResponse(r2.presign(key), status_code=302)
        return FileResponse(
            poster,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html; charset=utf-8")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
