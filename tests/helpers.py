"""
Shared helpers for the infrastructure test-suite.

Tests use the standard library ``unittest`` runner only, because the project
environment does not ship pytest.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path


class TempDirMixin:
    """Give each test its own throwaway directory tree."""

    def setUp(self):
        super().setUp()
        self.temp_root = Path(tempfile.mkdtemp(prefix="autoclips_test_"))

    def tearDown(self):
        shutil.rmtree(str(self.temp_root), ignore_errors=True)
        super().tearDown()

    def dir(self, *parts) -> Path:
        target = self.temp_root.joinpath(*parts)
        target.mkdir(parents=True, exist_ok=True)
        return target


def make_video(path, seconds: float = 2.0, size: str = "640x360") -> Path:
    """Render a real, decodable MP4 so validation is exercised for real."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "testsrc=size=%s:rate=30:duration=%s" % (size, seconds),
        "-f", "lavfi", "-i", "sine=frequency=440:duration=%s" % seconds,
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-shortest", str(path),
    ]
    subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if not path.exists() or path.stat().st_size == 0:
        raise RuntimeError("test fixture could not be generated: %s" % path)
    return path