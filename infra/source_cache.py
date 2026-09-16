"""
Deduplicating, crash-safe source cache.

The cache identity is a stable id derived from the source itself (for KICK, the
clip or VOD id). It is never a random job id and never the requesting user.
That is what allows many users to ask for the same source at the same time and
pay for exactly one download.

Concurrency contract (double-checked locking)::

    lookup -> MISS
      acquire the cross-process lock for this source id
      lookup AGAIN           <- another worker may have finished while we waited
      download into a private staging file
      validate (size + decodability)
      os.replace() to the canonical path   <- atomic publication
      release lock

A second request for the same source waits on that lock, then finds a valid
entry, so it can never observe a partially written file.

Jobs never delete cache entries: ``link_into`` gives each job its own name for
the cached file (hard link when possible, copy otherwise), which keeps the
existing per-job cleanup code correct while protecting shared state.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple
from urllib.parse import urlparse

from filelock import Timeout as FileLockTimeout

from .config import INFRA_CONFIG
from .locking import (
    LockBusyError,
    SourceValidationError,
    atomic_publish,
    link_or_copy,
    safe_rmtree,
    safe_unlink,
    source_lock,
    validate_media,
)
from .logutil import (
    EVENT_DOWNLOAD_CACHE_HIT,
    EVENT_DOWNLOAD_COMPLETED,
    EVENT_DOWNLOAD_STARTED,
    EVENT_CLEANUP_COMPLETED,
    log_event,
)

CACHE_VERSION = 1

_KICK_CLIP_RE = re.compile(r"/clips?/([A-Za-z0-9_-]+)", re.IGNORECASE)
_KICK_VOD_RE = re.compile(r"/videos?/([A-Za-z0-9_-]+)", re.IGNORECASE)


def canonical_source_id(url: str) -> str:
    """Derive a stable cache identity from a media URL.

    KICK clip: https://kick.com/<streamer>/clips/<clip_id> -> kick:clip:<id>
    KICK VOD:  https://kick.com/<streamer>/videos/<vod_id>  -> kick:vod:<id>

    Anything else falls back to a host+path slug, which is still stable for the
    same URL and never involves the requesting user.
    """
    text = (url or "").strip()
    if not text:
        raise ValueError("cannot derive a source id from an empty URL")

    try:
        parsed = urlparse(text)
    except Exception as error:  # pragma: no cover - urlparse is very tolerant
        raise ValueError("unparseable source URL: %s" % error)

    host = (parsed.hostname or "").lower()
    path = parsed.path or ""

    if "kick.com" in host:
        match = _KICK_CLIP_RE.search(path)
        if match:
            return "kick:clip:%s" % match.group(1).lower()
        match = _KICK_VOD_RE.search(path)
        if match:
            return "kick:vod:%s" % match.group(1).lower()
        segments = [segment for segment in path.split("/") if segment]
        if len(segments) >= 2:
            return "kick:%s" % "-".join(segments).lower()

    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", "%s%s" % (host, path)).strip("-").lower()
    return "url:%s" % (slug[:180] or "unknown")


def _safe_segment(source_id: str) -> str:
    """Make a source id safe to use as a filesystem name."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", source_id)[:120] or "unknown"


class SourceCache:
    """One download per source, shared safely by every job."""

    def __init__(
        self,
        root: Optional[Path] = None,
        ttl_seconds: Optional[int] = None,
        min_bytes: Optional[int] = None,
    ):
        self.root = Path(root) if root else Path(INFRA_CONFIG.source_cache_dir)
        self.ttl_seconds = INFRA_CONFIG.cache_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        self.min_bytes = INFRA_CONFIG.source_min_bytes if min_bytes is None else int(min_bytes)
        self.staging_dir = self.root / "tmp"
        self.root.mkdir(parents=True, exist_ok=True)
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def entry_dir(self, source_id: str) -> Path:
        return self.root / _safe_segment(source_id)

    def meta_path(self, source_id: str) -> Path:
        return self.entry_dir(source_id) / "meta.json"

    def lock_path(self, source_id: str) -> Path:
        return self.root / ("%s.lock" % _safe_segment(source_id))

    def find_media(self, source_id: str) -> Optional[Path]:
        entry = self.entry_dir(source_id)
        if not entry.is_dir():
            return None
        for candidate in sorted(entry.glob("source.*")):
            if candidate.is_file() and not candidate.name.endswith(".part"):
                return candidate
        return None

    # ------------------------------------------------------------- inspection
    def lookup(self, source_id: str, log_hit: bool = True) -> Optional[Path]:
        """Return a validated cached path, or None when there is no usable entry."""
        media = self.find_media(source_id)
        if media is None:
            return None

        ok, reason, duration = validate_media(media, min_bytes=self.min_bytes)
        if not ok:
            log_event("CACHE_ENTRY_INVALID", source_id=source_id, reason=reason, path=str(media))
            safe_unlink(media)
            safe_unlink(self.meta_path(source_id))
            return None

        if log_hit:
            log_event(
                EVENT_DOWNLOAD_CACHE_HIT,
                source_id=source_id,
                path=str(media),
                duration=duration,
            )
        return media

    def read_meta(self, source_id: str) -> Dict:
        try:
            return json.loads(self.meta_path(source_id).read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_meta(self, source_id: str, url: str, media: Path, duration: float) -> None:
        payload = {
            "version": CACHE_VERSION,
            "source_id": source_id,
            "url": url,
            "file": media.name,
            "bytes": media.stat().st_size,
            "duration": duration,
            "created_at": time.time(),
        }
        try:
            self.meta_path(source_id).write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    # ------------------------------------------------------------ acquisition
    def fetch(
        self,
        url: str,
        producer: Callable[[Path], Optional[Path]],
        source_id: Optional[str] = None,
        job_id: Optional[str] = None,
        suffix: str = ".mp4",
        force: bool = False,
    ) -> Path:
        """Return a validated cached media path, downloading only when needed.

        ``producer`` is handed a destination file path inside a private staging
        directory and must create that file (or raise). It runs while the source
        lock is held, so duplicate downloads for one source are impossible.
        """
        if source_id is None:
            source_id = canonical_source_id(url)

        if not force:
            cached = self.lookup(source_id)
            if cached is not None:
                return cached

        lock = source_lock(self.lock_path(source_id))
        try:
            lock.acquire()
        except FileLockTimeout:
            raise LockBusyError("timed out waiting for the source lock: %s" % source_id)

        try:
            # Double check: someone else may have published while we waited.
            if not force:
                cached = self.lookup(source_id)
                if cached is not None:
                    return cached

            log_event(EVENT_DOWNLOAD_STARTED, job_id=job_id, source_id=source_id, url=url)
            staged, staging_root = self._stage(source_id, producer, suffix)
            try:
                ok, reason, duration = validate_media(staged, min_bytes=self.min_bytes)
                if not ok:
                    raise SourceValidationError(
                        "downloaded source failed validation (%s) for %s" % (reason, source_id)
                    )

                entry = self.entry_dir(source_id)
                entry.mkdir(parents=True, exist_ok=True)
                final = entry / ("source%s" % (staged.suffix or suffix))
                published = atomic_publish(staged, final)
                self._write_meta(source_id, url, published, duration)
                log_event(
                    EVENT_DOWNLOAD_COMPLETED,
                    job_id=job_id,
                    source_id=source_id,
                    path=str(published),
                    duration=duration,
                    bytes=published.stat().st_size,
                )
                return published
            finally:
                safe_rmtree(staging_root)
        finally:
            try:
                lock.release()
            except Exception:
                pass

    def _stage(
        self,
        source_id: str,
        producer: Callable[[Path], Optional[Path]],
        suffix: str,
    ) -> Tuple[Path, Path]:
        staging_root = self.staging_dir / (
            "%s_%s" % (_safe_segment(source_id), uuid.uuid4().hex[:8])
        )
        staging_root.mkdir(parents=True, exist_ok=True)
        destination = staging_root / ("source%s" % suffix)
        try:
            produced = producer(destination)
            produced = Path(produced) if produced else destination
            if not produced.exists() or not produced.is_file():
                raise SourceValidationError("producer did not create a file: %s" % produced)
            return produced, staging_root
        except Exception:
            safe_rmtree(staging_root)
            raise

    def link_into(self, source_id: str, destination) -> Path:
        """Give a job its own name for the shared cached file.

        The job owns the returned path, so its cleanup cannot affect the cache
        entry or any other job.
        """
        media = self.lookup(source_id, log_hit=False)
        if media is None:
            raise SourceValidationError("no usable cached source for %s" % source_id)
        return link_or_copy(media, destination)

    def get_for_job(
        self,
        url: str,
        producer: Callable[[Path], Optional[Path]],
        destination,
        job_id: Optional[str] = None,
        suffix: str = ".mp4",
    ) -> Tuple[Path, str]:
        """Fetch (deduplicated) and hand the job an isolated copy of the source.

        Returns ``(job_local_path, source_id)``.
        """
        source_id = canonical_source_id(url)
        cached = self.fetch(url, producer, source_id=source_id, job_id=job_id, suffix=suffix)
        return self.link_into(source_id, destination), source_id

    # ------------------------------------------------------------- maintenance
    def invalidate(self, source_id: str) -> bool:
        """Drop one cache entry. Only ever called outside an active fetch."""
        return safe_rmtree(self.entry_dir(source_id))

    def sweep_expired(self, ttl_seconds: Optional[int] = None, active_source_ids=None) -> Dict[str, int]:
        """Remove cache entries older than the TTL.

        An entry is only removed while holding its lock, and only when it is not
        in ``active_source_ids``. Hard links already handed to running jobs keep
        working, so removal can never break an in-flight job.
        """
        ttl = self.ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        active = set(active_source_ids or ())
        removed = 0
        bytes_freed = 0
        now = time.time()

        self._sweep_staging()

        if not self.root.exists():
            return {"removed": 0, "bytes_freed": 0}

        for entry in list(self.root.iterdir()):
            if not entry.is_dir() or entry.name == "tmp":
                continue
            if entry.name in {_safe_segment(sid) for sid in active}:
                continue
            media = self.find_media_by_dir(entry)
            if media is None:
                continue
            try:
                if ttl and (now - media.stat().st_mtime) <= ttl:
                    continue
            except OSError:
                continue

            lock_path = self.root / ("%s.lock" % entry.name)
            lock = source_lock(lock_path, timeout=1)
            try:
                lock.acquire()
            except FileLockTimeout:
                continue
            try:
                size = media.stat().st_size if media.exists() else 0
                if safe_rmtree(entry):
                    removed += 1
                    bytes_freed += size
            finally:
                try:
                    lock.release()
                except Exception:
                    pass
            safe_unlink(lock_path)

        if removed:
            log_event(EVENT_CLEANUP_COMPLETED, scope="source_cache",
                      removed=removed, bytes_freed=bytes_freed)
        return {"removed": removed, "bytes_freed": bytes_freed}

    def find_media_by_dir(self, entry: Path) -> Optional[Path]:
        try:
            for candidate in sorted(entry.glob("source.*")):
                if candidate.is_file() and not candidate.name.endswith(".part"):
                    return candidate
        except OSError:
            return None
        return None

    def _sweep_staging(self, max_age_seconds: int = 21600) -> int:
        """Remove abandoned staging directories left behind by a crash."""
        removed = 0
        if not self.staging_dir.exists():
            return removed
        now = time.time()
        for entry in list(self.staging_dir.iterdir()):
            try:
                if entry.is_dir() and (now - entry.stat().st_mtime) > max_age_seconds:
                    if safe_rmtree(entry):
                        removed += 1
            except OSError:
                continue
        return removed

    def stats(self) -> Dict[str, int]:
        entries = 0
        total_bytes = 0
        if self.root.exists():
            for entry in self.root.iterdir():
                if not entry.is_dir() or entry.name == "tmp":
                    continue
                media = self.find_media_by_dir(entry)
                if media is not None:
                    entries += 1
                    try:
                        total_bytes += media.stat().st_size
                    except OSError:
                        pass
        return {"entries": entries, "bytes": total_bytes}


_CACHE: Optional[SourceCache] = None


def get_source_cache() -> SourceCache:
    """Process-wide source cache instance."""
    global _CACHE
    if _CACHE is None:
        _CACHE = SourceCache()
    return _CACHE