"""
Atomic filesystem helpers plus media validation.

These rules are what keep the shared cache safe:

* a file only ever appears at its canonical cache path through
  ``os.replace``, which is atomic on one volume, so a partially written file
  can never be mistaken for a finished one;
* readers validate size *and* decodability before trusting any file;
* deletion retries, because Windows scanners and media players can hold a
  handle for a moment after a process exits.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple

from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from .config import INFRA_CONFIG


class SourceValidationError(RuntimeError):
    """Raised when a media file is missing, truncated or unreadable."""


class LockBusyError(RuntimeError):
    """Raised when another worker holds the lock for too long."""


def probe_duration(path, ffprobe_timeout: Optional[int] = None) -> float:
    """Return media duration in seconds, or 0.0 when it cannot be read."""
    timeout = ffprobe_timeout or INFRA_CONFIG.ffprobe_timeout
    command = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout,
        )
    except Exception:
        return 0.0
    try:
        return float(result.stdout.strip())
    except (TypeError, ValueError):
        return 0.0


def validate_media(
    path,
    min_bytes: Optional[int] = None,
    min_duration: Optional[float] = None,
    max_duration: Optional[float] = None,
) -> Tuple[bool, str, float]:
    """Check that ``path`` is a complete, decodable media file.

    Returns ``(ok, reason, duration_seconds)``.
    """
    if min_bytes is None:
        min_bytes = INFRA_CONFIG.source_min_bytes
    if min_duration is None:
        min_duration = INFRA_CONFIG.source_min_duration
    if max_duration is None:
        max_duration = INFRA_CONFIG.source_max_duration

    path = Path(path)
    if not path.exists():
        return False, "missing", 0.0
    if not path.is_file():
        return False, "not-a-file", 0.0
    try:
        size = path.stat().st_size
    except OSError as error:
        return False, "stat-failed:%s" % error, 0.0
    if size < min_bytes:
        return False, "too-small:%d" % size, 0.0

    duration = probe_duration(path)
    if duration < min_duration:
        return False, "invalid-duration:%s" % duration, duration
    if max_duration and duration > max_duration:
        return False, "too-long:%s" % duration, duration
    return True, "ok", duration


def atomic_publish(source, destination) -> Path:
    """Atomically move ``source`` onto ``destination``."""
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(str(source), str(destination))
    except OSError:
        shutil.move(str(source), str(destination))
    return destination


def link_or_copy(source, destination) -> Path:
    """Give ``destination`` its own name for ``source``.

    A hard link is preferred: it costs no extra disk space and the caller can
    delete its own name without touching the shared cache entry. When the
    volume does not support links the file is copied instead.
    """
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return destination
    try:
        os.link(str(source), str(destination))
        return destination
    except (OSError, NotImplementedError, AttributeError):
        pass
    shutil.copy2(str(source), str(destination))
    return destination


def safe_unlink(path, retries: int = 5, delay: float = 0.4) -> bool:
    """Delete a file, retrying briefly on Windows lock errors."""
    path = Path(path)
    for attempt in range(retries):
        try:
            if not path.exists():
                return True
            path.unlink()
            return True
        except PermissionError:
            time.sleep(delay)
        except OSError:
            time.sleep(delay)
        except Exception:
            return False
    try:
        path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def safe_rmtree(path, retries: int = 5, delay: float = 0.4) -> bool:
    """Delete a directory tree, tolerating transient Windows locks."""
    path = Path(path)
    if not path.exists():
        return True
    for attempt in range(retries):
        try:
            shutil.rmtree(str(path))
            return True
        except (PermissionError, OSError):
            time.sleep(delay)
        except Exception:
            return False
    shutil.rmtree(str(path), ignore_errors=True)
    return not path.exists()


def source_lock(lock_path, timeout: Optional[float] = None) -> FileLock:
    """Build a cross-process lock for one cache entry."""
    return FileLock(
        str(lock_path),
        timeout=INFRA_CONFIG.cache_lock_timeout if timeout is None else timeout,
    )


def file_sha256(path, chunk_size: int = 1 << 20) -> str:
    """Streaming SHA-256 used for diagnostics and duplicate detection."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()