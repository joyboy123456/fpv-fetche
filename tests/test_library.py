from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from web.library import item_from_relpath, safe_file, scan


class ScanTests(unittest.TestCase):
    def test_scan_category_date_title(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "amateur" / "2026-09-21" / "hello_world_720p.mp4"
            video.parent.mkdir(parents=True)
            video.write_bytes(b"fake-mp4-content-not-empty")
            (root / ".history.json").write_text("[]")
            (root / "amateur" / "2026-09-21" / "skip.mp4.part").write_bytes(b"x" * 10)

            items = scan(root)
            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item.category, "amateur")
            self.assertEqual(item.date, "2026-09-21")
            self.assertEqual(item.title, "hello_world")
            self.assertEqual(item.resolution, "720")
            self.assertEqual(item.relpath, "amateur/2026-09-21/hello_world_720p.mp4")

    def test_safe_file_blocks_traversal(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            video = root / "solo" / "clip.mp4"
            video.parent.mkdir()
            video.write_bytes(b"abc")
            outside = Path(tmp).parent / "secret.txt"
            self.assertIsNone(safe_file(root, "../secret.txt"))
            self.assertIsNone(safe_file(root, "/etc/passwd"))
            self.assertIsNone(safe_file(root, "solo/nope.txt"))
            self.assertEqual(safe_file(root, "solo/clip.mp4"), video.resolve())

    def test_item_from_relpath(self) -> None:
        item = item_from_relpath(
            "amateur/2026-09-21/hello_world_720p.mp4", 1024, 1.0
        )
        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.title, "hello_world")
        self.assertEqual(item.resolution, "720")
        self.assertIsNone(item_from_relpath(".thumbs/abc.jpg", 10, 1.0))
        self.assertIsNone(item_from_relpath("amateur/x.mp4.part", 10, 1.0))


if __name__ == "__main__":
    unittest.main()
