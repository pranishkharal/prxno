"""
Versioned analysis / transcript cache (the second cache level).

The VOD pipeline previously wrote ``transcript_<job_id>.json`` and
``analysis_<job_id>.json``. Because ``job_id`` is a random UUID, two jobs could
never share a result even when they analysed the identical source.

This module keys results by the source plus the processing identity::

    key = sha256(source_id | kind | model | version | config_fingerprint)

So two jobs on one source share a single transcript/analysis, while changing the
model, the processing version, or the analysis configuration invalidates old
results instead of silently reusing incompatible data.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from filelock import Timeout as FileLockTimeout

from .config import INFRA_CONFIG
from .locking import atomic_publish, safe_rmtree, safe_unlink, source_lock
from .logutil import (
    EVENT_ANALYSIS_CACHE_HIT,
    EVENT_ANALYSIS_COMPLETED,
    EVENT_ANALYSIS_STARTED,
    EVENT_CLEANUP_COMPLETED,
    log_event,
)

ANALYSIS_CACHE_VERSION = "1.0.0"

KIND_TRANSCRIPT = "transcript"
KIND_ANALYSIS = "analysis"
KIND_CHAT = "chat"


def _safe_segment(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value))[:120] or "unknown"


def config_fingerprint(config: Any) -> str:
    """Stable hash of an analysis configuration.

    Accepts a dict, a dataclass instance, or None. Unstable values such as
    ``id()`` are never used, so the same logical configuration always yields the
    same fingerprint across processes and restarts.
    """
    if config is None:
        payload: Any = {}
    elif isinstance(config, dict):
        payload = config
    elif hasattr(config, "__dict__"):
        payload = {k: v for k, v in vars(config).items() if not k.startswith("_")}
    else:
        payload = {"repr": repr(config)}

    try:
        blob = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        blob = repr(payload)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def analysis_key(
    source_id: str,
    kind: str,
    model: Optional[str] = None,
    version: str = ANALYSIS_CACHE_VERSION,
    config: Any = None,
) -> str:
    """Build the stable cache key for one (source, kind, processing) triple."""
    parts = [
        str(source_id),
        str(kind),
        str(model or "default"),
        str(version),
        config_fingerprint(config),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


class AnalysisCache:
    """Shared, versioned results for one analysed source."""

    def __init__(self, root: Optional[Path] = None, ttl_seconds: Optional[int] = None):
        self.root = Path(root) if root else Path(INFRA_CONFIG.analysis_cache_dir)
        self.ttl_seconds = INFRA_CONFIG.cache_ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        self.root.mkdir(parents=True, exist_ok=True)
        # In-process, per-key asyncio locks: they keep many concurrent callers
        # from occupying many worker threads while waiting on the file lock.
        self._lock_guard = threading.Lock()
        self._async_locks = {}
        self._async_lock_loop = None

    # ------------------------------------------------------------------ paths
    def source_dir(self, source_id: str) -> Path:
        return self.root / _safe_segment(source_id)

    def path_for(self, source_id: str, kind: str, key: str) -> Path:
        return self.source_dir(source_id) / ("%s__%s.json" % (_safe_segment(kind), key[:12]))

    def lock_path(self, source_id: str, kind: str) -> Path:
        return self.root / ("%s__%s.lock" % (_safe_segment(source_id), _safe_segment(kind)))

    # -------------------------------------------------------------- accessors
    def get(
        self,
        source_id: str,
        kind: str,
        model: Optional[str] = None,
        version: str = ANALYSIS_CACHE_VERSION,
        config: Any = None,
        log_hit: bool = True,
    ) -> Optional[Any]:
        key = analysis_key(source_id, kind, model, version, config)
        path = self.path_for(source_id, kind, key)
        if not path.exists():
            return None

        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            safe_unlink(path)
            return None

        if envelope.get("key") != key:
            # Left over from a different model/config/version.
            safe_unlink(path)
            return None
        if envelope.get("version") != ANALYSIS_CACHE_VERSION and version == ANALYSIS_CACHE_VERSION:
            safe_unlink(path)
            return None

        if log_hit:
            log_event(
                EVENT_ANALYSIS_CACHE_HIT,
                source_id=source_id,
                kind=kind,
                key=key[:12],
                path=str(path),
            )
        return envelope.get("payload")

    def put(
        self,
        source_id: str,
        kind: str,
        payload: Any,
        model: Optional[str] = None,
        version: str = ANALYSIS_CACHE_VERSION,
        config: Any = None,
    ) -> Path:
        key = analysis_key(source_id, kind, model, version, config)
        target = self.path_for(source_id, kind, key)
        target.parent.mkdir(parents=True, exist_ok=True)

        envelope = {
            "version": version,
            "source_id": source_id,
            "kind": kind,
            "model": model or "default",
            "config_fingerprint": config_fingerprint(config),
            "key": key,
            "created_at": time.time(),
            "payload": payload,
        }

        staging = target.parent / ("%s.tmp-%s" % (target.stem, uuid.uuid4().hex[:8]))
        try:
            staging.write_text(
                json.dumps(envelope, default=str, ensure_ascii=False), encoding="utf-8"
            )
            atomic_publish(staging, target)
        finally:
            safe_unlink(staging)
        return target

    def get_or_compute(
        self,
        source_id: str,
        kind: str,
        compute: Callable[[], Any],
        model: Optional[str] = None,
        version: str = ANALYSIS_CACHE_VERSION,
        config: Any = None,
        job_id: Optional[str] = None,
    ) -> Tuple[Any, bool]:
        """Return ``(payload, was_cache_hit)``, computing only on a miss.

        ``compute`` runs while holding the per-(source, kind) lock, so twenty
        simultaneous jobs on one source produce one transcription, not twenty.
        """
        cached = self.get(source_id, kind, model, version, config)
        if cached is not None:
            return cached, True

        lock = source_lock(self.lock_path(source_id, kind))
        acquired = False
        try:
            try:
                lock.acquire()
                acquired = True
            except FileLockTimeout:
                # Somebody else is computing it. Compute locally rather than
                # failing the job; correctness is preserved either way.
                log_event(
                    "ANALYSIS_LOCK_BUSY", job_id=job_id, source_id=source_id, kind=kind
                )
                return compute(), False

            cached = self.get(source_id, kind, model, version, config)
            if cached is not None:
                return cached, True

            log_event(EVENT_ANALYSIS_STARTED, job_id=job_id, source_id=source_id, kind=kind)
            payload = compute()
            self.put(source_id, kind, payload, model=model, version=version, config=config)
            log_event(
                EVENT_ANALYSIS_COMPLETED,
                job_id=job_id,
                source_id=source_id,
                kind=kind,
            )
            return payload, False
        finally:
            if acquired:
                try:
                    lock.release()
                except Exception:
                    pass

    def _async_lock(self, key: str):
        """Return the asyncio lock guarding one cache key in this process."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        with self._lock_guard:
            if self._async_lock_loop is not loop:
                # A different event loop is running (tests, a restart): locks
                # created for a dead loop must never be reused.
                self._async_locks.clear()
                self._async_lock_loop = loop
            lock = self._async_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._async_locks[key] = lock
            return lock

    @staticmethod
    async def _await_compute(compute):
        """Call ``compute``, awaiting it when it is a coroutine function."""
        result = compute()
        if asyncio.iscoroutine(result):
            result = await result
        return result

    async def get_or_compute_async(
        self,
        source_id: str,
        kind: str,
        compute,
        model: Optional[str] = None,
        version: str = ANALYSIS_CACHE_VERSION,
        config: Any = None,
        job_id: Optional[str] = None,
    ) -> Tuple[Any, bool]:
        """Async twin of ``get_or_compute``.

        ``compute`` may be a coroutine function (transcription is). Blocking
        cache reads, the atomic write and the cross-process lock are all pushed
        to worker threads, so the Discord event loop is never blocked.
        """
        key = analysis_key(source_id, kind, model, version, config)

        async with self._async_lock(key):
            cached = self.get(source_id, kind, model, version, config)
            if cached is not None:
                return cached, True

            lock = source_lock(self.lock_path(source_id, kind))
            acquired = False
            try:
                try:
                    await asyncio.to_thread(lock.acquire)
                    acquired = True
                except FileLockTimeout:
                    # Another process is computing it. Compute locally rather
                    # than failing the job; correctness is preserved either way.
                    log_event(
                        "ANALYSIS_LOCK_BUSY",
                        job_id=job_id,
                        source_id=source_id,
                        kind=kind,
                    )
                    return await self._await_compute(compute), False

                cached = self.get(source_id, kind, model, version, config)
                if cached is not None:
                    return cached, True

                log_event(
                    EVENT_ANALYSIS_STARTED,
                    job_id=job_id,
                    source_id=source_id,
                    kind=kind,
                )
                payload = await self._await_compute(compute)
                await asyncio.to_thread(
                    self.put, source_id, kind, payload, model, version, config
                )
                log_event(
                    EVENT_ANALYSIS_COMPLETED,
                    job_id=job_id,
                    source_id=source_id,
                    kind=kind,
                )
                return payload, False
            finally:
                if acquired:
                    try:
                        await asyncio.to_thread(lock.release)
                    except Exception:
                        pass

    # ------------------------------------------------------------ maintenance
    def invalidate_source(self, source_id: str) -> bool:
        return safe_rmtree(self.source_dir(source_id))

    def sweep_expired(self, ttl_seconds: Optional[int] = None) -> Dict[str, int]:
        ttl = self.ttl_seconds if ttl_seconds is None else int(ttl_seconds)
        removed = 0
        bytes_freed = 0
        now = time.time()
        if not self.root.exists():
            return {"removed": 0, "bytes_freed": 0}

        for entry in list(self.root.iterdir()):
            if not entry.is_dir():
                continue
            try:
                newest = max((f.stat().st_mtime for f in entry.glob("*.json")), default=0)
            except OSError:
                continue
            if ttl and newest and (now - newest) <= ttl:
                continue
            try:
                size = sum(f.stat().st_size for f in entry.glob("*.json"))
            except OSError:
                size = 0
            if safe_rmtree(entry):
                removed += 1
                bytes_freed += size

        if removed:
            log_event(
                EVENT_CLEANUP_COMPLETED,
                scope="analysis_cache",
                removed=removed,
                bytes_freed=bytes_freed,
            )
        return {"removed": removed, "bytes_freed": bytes_freed}

    def stats(self) -> Dict[str, int]:
        entries = 0
        total_bytes = 0
        if self.root.exists():
            for entry in self.root.rglob("*.json"):
                entries += 1
                try:
                    total_bytes += entry.stat().st_size
                except OSError:
                    pass
        return {"entries": entries, "bytes": total_bytes}


_CACHE: Optional[AnalysisCache] = None


def get_analysis_cache() -> AnalysisCache:
    """Process-wide analysis cache instance."""
    global _CACHE
    if _CACHE is None:
        _CACHE = AnalysisCache()
    return _CACHE