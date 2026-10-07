from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from fetcher import Config
from web.app import create_app


def _cfg(root: Path, password: str = "") -> Config:
    return Config(
        download_root=root,
        categories=[],
        history_file=root / ".history.json",
        web_password=password,
        web_secret="test-secret",
    )


class WebAppTests(unittest.TestCase):
    def test_library_and_range(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "amateur" / "2026-09-21" / "clip_720p.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"0123456789abcdef")
            client = TestClient(create_app(_cfg(root)))

            meta = client.get("/api/meta")
            self.assertEqual(meta.status_code, 200)
            self.assertFalse(meta.json()["auth_required"])

            lib = client.get("/api/library")
            self.assertEqual(lib.status_code, 200)
            body = lib.json()
            self.assertEqual(body["total"], 1)
            self.assertEqual(body["items"][0]["title"], "clip")

            streamed = client.get(
                "/media",
                params={"path": "amateur/2026-09-21/clip_720p.mp4"},
                headers={"Range": "bytes=0-3"},
            )
            self.assertEqual(streamed.status_code, 206)
            self.assertEqual(streamed.content, b"0123")
            self.assertEqual(streamed.headers.get("content-range"), "bytes 0-3/16")

            denied = client.get("/media", params={"path": "../clip_720p.mp4"})
            self.assertEqual(denied.status_code, 404)

            missing = client.get("/thumb", params={"path": "nope.mp4"})
            self.assertEqual(missing.status_code, 404)

    def test_password_gate(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = TestClient(create_app(_cfg(root, password="letmein")))
            self.assertEqual(client.get("/api/library").status_code, 401)
            self.assertEqual(client.get("/thumb", params={"path": "x.mp4"}).status_code, 401)
            bad = client.post("/api/login", json={"password": "nope"})
            self.assertEqual(bad.status_code, 403)
            ok = client.post("/api/login", json={"password": "letmein"})
            self.assertEqual(ok.status_code, 200)
            self.assertEqual(client.get("/api/library").status_code, 200)
            self.assertEqual(client.get("/thumb", params={"path": "x.mp4"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
