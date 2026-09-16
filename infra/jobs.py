"""
Durable job store and resource-aware queue.

The clip/edit commands previously used ``asyncio.create_task(run_pipeline())``
with an in-memory ``SESSIONS`` dict. Nothing survived a restart, no job could be
inspected after the fact, and ``!cancel`` only dropped UI state without stopping
any real work.

This module provides:

* ``JobRecord`` - an explicit, serialisable unit of work (job id, user, source
  id, kind, parameters, status, attempts, error, output),
* ``JobStore`` - atomic JSON persistence (one file per job),
* ``ResourceQueue`` - per-resource-class execution that enforces the governor\'s
  limits, retries transient failures with backoff, supports real cancellation,
  and repairs jobs orphaned by a crash.

No Redis is required for single-machine operation. ``JobStore`` is deliberately
backend-shaped so a remote store can replace it for multi-machine workers.
"""

from __future__ import annotations

import asyncio
import json
import platform
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .concurrency import CLASS_ANALYSIS, CLASS_DOWNLOAD, CLASS_ENCODE, get_governor
from .config import INFRA_CONFIG
from .locking import atomic_publish, safe_unlink
from .logutil import (
    EVENT_JOB_CANCELLED,
    EVENT_JOB_COMPLETED,
    EVENT_JOB_CREATED,
    EVENT_JOB_FAILED,
    EVENT_JOB_QUEUED,
    EVENT_JOB_STARTED,
    log_event,
)


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)


class JobKind(str, Enum):
    CLIP_FETCH = "clip_fetch"
    CLIP_EDIT = "clip_edit"
    VOD_ANALYSIS = "vod_analysis"
    TRANSCRIPTION = "transcription"
    UPLOAD = "upload"


class JobCancelledError(RuntimeError):
    """Raised when a job is cancelled while it runs."""


_TRANSIENT_HINTS = (
    "timed out", "timeout", "connection", "reset by peer", "temporarily",
    "429", "502", "503", "504", "winerror 32", "winerror 5",
    "being used by another process", "locked", "unavailable", "network",
    "incomplete read", "remote end closed",
)


def is_transient(error: BaseException) -> bool:
    """Classify an error as retryable or permanent."""
    message = str(error).lower()
    return any(hint in message for hint in _TRANSIENT_HINTS)


def new_job_id(prefix: str = "job") -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:10])


@dataclass
class JobRecord:
    """Everything needed to describe, retry and report one job."""

    job_id: str
    kind: str = JobKind.CLIP_EDIT.value
    user_id: Optional[int] = None
    channel_id: Optional[int] = None
    source_id: Optional[str] = None
    source_url: Optional[str] = None
    params: Dict[str, Any] = field(default_factory=dict)
    status: str = JobStatus.QUEUED.value
    attempts: int = 0
    max_attempts: int = INFRA_CONFIG.job_max_attempts
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    output: Optional[str] = None
    progress: float = 0.0
    step: str = ""
    worker: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "JobRecord":
        known = set(cls.__dataclass_fields__)
        return cls(**{key: value for key, value in data.items() if key in known})

    def touch(self) -> None:
        self.updated_at = time.time()

    def mark(self, status: JobStatus, error: Optional[str] = None) -> "JobRecord":
        self.status = status.value
        if error:
            self.error = error
            self.error_type = type(error).__name__ if not isinstance(error, str) else "Error"
        if status is JobStatus.RUNNING:
            self.attempts += 1
            if self.started_at is None:
                self.started_at = time.time()
        if status.terminal:
            self.finished_at = time.time()
        self.touch()
        return self

    def progress_to(self, percent: float, step: str = "") -> "JobRecord":
        self.progress = max(0.0, min(100.0, float(percent)))
        if step:
            self.step = step
        self.touch()
        return self


class JobStore:
    """Atomic, one-file-per-job persistence."""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root) if root else Path(INFRA_CONFIG.project_root) / "vod_jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path_for(self, job_id: str) -> Path:
        return self.root / ("%s.json" % job_id)

    def save(self, record: JobRecord) -> Path:
        target = self.path_for(record.job_id)
        staging = target.with_suffix(".json.tmp")
        with self._lock:
            staging.write_text(
                json.dumps(record.to_dict(), indent=2, default=str), encoding="utf-8"
            )
            atomic_publish(staging, target)
        return target

    def load(self, job_id: str) -> Optional[JobRecord]:
        path = self.path_for(job_id)
        if not path.exists():
            return None
        try:
            return JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return None

    def all(self) -> List[JobRecord]:
        records: List[JobRecord] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                records.append(JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except Exception:
                continue
        return records

    def delete(self, job_id: str) -> bool:
        return safe_unlink(self.path_for(job_id))

    def repair_orphans(self) -> Dict[str, List[str]]:
        """Reconcile jobs left RUNNING by a crash.

        A job with attempts left is returned to QUEUED so it can run again; one
        that has exhausted its attempts is failed instead of looping forever.
        """
        requeued: List[str] = []
        failed: List[str] = []
        for record in self.all():
            if record.status not in (JobStatus.RUNNING.value, JobStatus.QUEUED.value):
                continue
            if record.status == JobStatus.RUNNING.value:
                if record.attempts < record.max_attempts:
                    record.mark(JobStatus.QUEUED)
                    record.step = "requeued after restart"
                    self.save(record)
                    requeued.append(record.job_id)
                else:
                    record.mark(JobStatus.FAILED, "interrupted by restart")
                    self.save(record)
                    failed.append(record.job_id)
        if requeued or failed:
            log_event("RESTART_RECOVERY", requeued=len(requeued), failed=len(failed))
        return {"requeued": requeued, "failed": failed}

    def sweep_terminal(self, keep_seconds: Optional[int] = None) -> int:
        """Delete finished job files older than the retention window."""
        keep = INFRA_CONFIG.cache_ttl_seconds if keep_seconds is None else int(keep_seconds)
        cutoff = time.time() - keep
        removed = 0
        for record in self.all():
            if record.status not in (
                JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value
            ):
                continue
            stamp = record.finished_at if record.finished_at is not None else record.updated_at
            if stamp < cutoff and self.delete(record.job_id):
                removed += 1
        return removed


class ResourceQueue:
    """Runs job coroutines under per-class limits with retry and cancellation."""

    def __init__(self, store: Optional[JobStore] = None, governor=None):
        self.store = store or JobStore()
        self.governor = governor or get_governor()
        self._tasks: Dict[str, asyncio.Task] = {}
        self._cancelled: set = set()
        self._lock = threading.RLock()
        self.worker = "%s-%d" % (platform.node() or "worker", __import__("os").getpid())

    # ------------------------------------------------------------- lifecycle
    def create(self, kind: str, job_id: Optional[str] = None, **kwargs) -> JobRecord:
        """Create and persist a job record.

        Pass ``job_id`` to line the infrastructure record up with an
        application-level job id so logs, status messages and cancellation all
        refer to the same job.
        """
        record = JobRecord(
            job_id=job_id or new_job_id(str(kind)), kind=str(kind), **kwargs
        )
        record.worker = self.worker
        self.store.save(record)
        log_event(
            EVENT_JOB_CREATED,
            job_id=record.job_id,
            kind=record.kind,
            user_id=record.user_id,
            source_id=record.source_id,
        )
        return record

    def _persist(self, record: JobRecord) -> None:
        try:
            self.store.save(record)
        except Exception:
            pass

    def is_cancelled(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelled

    def cancel(self, job_id: str) -> bool:
        """Cancel a queued or running job. Returns True when something changed."""
        with self._lock:
            self._cancelled.add(job_id)
            task = self._tasks.get(job_id)

        record = self.store.load(job_id)
        changed = False
        if record is not None and not JobStatus(record.status).terminal:
            record.mark(JobStatus.CANCELLED, "cancelled by user")
            self._persist(record)
            changed = True
            log_event(EVENT_JOB_CANCELLED, job_id=job_id)

        if task is not None and not task.done():
            task.cancel()
            changed = True

        # Cancelling an asyncio task cannot interrupt a worker thread, so any
        # FFmpeg process this job started has to be terminated explicitly.
        try:
            from .ffmpeg_runner import get_ffmpeg_runner

            if get_ffmpeg_runner().cancel(job_id):
                changed = True
        except Exception:
            pass

        return changed

    async def run(
        self,
        record: JobRecord,
        resource_class: str,
        func: Callable[[], Any],
        on_progress: Optional[Callable[[float, str], None]] = None,
    ) -> Any:
        """Await ``func`` under the class limit, retrying transient failures."""
        record.mark(JobStatus.QUEUED)
        self._persist(record)
        log_event(EVENT_JOB_QUEUED, job_id=record.job_id, resource=resource_class,
                  limit=self.governor.limit(resource_class))

        semaphore = self.governor.semaphore(resource_class)
        last_error: Optional[BaseException] = None

        async with semaphore:
            if self.is_cancelled(record.job_id):
                record.mark(JobStatus.CANCELLED, "cancelled before start")
                self._persist(record)
                raise JobCancelledError("job %s was cancelled" % record.job_id)

            for attempt in range(1, record.max_attempts + 1):
                record.mark(JobStatus.RUNNING)
                self._persist(record)
                log_event(
                    EVENT_JOB_STARTED,
                    job_id=record.job_id,
                    kind=record.kind,
                    attempt=attempt,
                    max_attempts=record.max_attempts,
                )
                try:
                    result = func()
                    if asyncio.iscoroutine(result):
                        result = await result
                    record.mark(JobStatus.COMPLETED)
                    record.progress_to(100, "done")
                    self._persist(record)
                    log_event(EVENT_JOB_COMPLETED, job_id=record.job_id, kind=record.kind)
                    return result
                except asyncio.CancelledError:
                    record.mark(JobStatus.CANCELLED, "cancelled")
                    self._persist(record)
                    log_event(EVENT_JOB_CANCELLED, job_id=record.job_id)
                    raise
                except Exception as error:
                    last_error = error
                    retryable = is_transient(error) and attempt < record.max_attempts
                    log_event(
                        EVENT_JOB_FAILED if not retryable else "JOB_RETRY",
                        job_id=record.job_id,
                        attempt=attempt,
                        error=str(error)[:400],
                        error_type=type(error).__name__,
                    )
                    if not retryable:
                        record.mark(JobStatus.FAILED, "%s: %s" % (type(error).__name__, error))
                        self._persist(record)
                        raise
                    record.progress_to(0, "retrying after: %s" % type(error).__name__)
                    self._persist(record)
                    await asyncio.sleep(INFRA_CONFIG.job_retry_backoff * attempt)

        raise last_error if last_error else RuntimeError("job %s failed" % record.job_id)

    def spawn(
        self,
        record: JobRecord,
        resource_class: str,
        func: Callable[[], Any],
        on_progress: Optional[Callable[[float, str], None]] = None,
    ) -> asyncio.Task:
        """Fire-and-forget variant that keeps a cancellable handle."""
        task = asyncio.create_task(self.run(record, resource_class, func, on_progress))
        with self._lock:
            self._tasks[record.job_id] = task

        def _done(_task):
            with self._lock:
                self._tasks.pop(record.job_id, None)

        task.add_done_callback(_done)
        return task

    def active_job_ids(self) -> List[str]:
        with self._lock:
            return list(self._tasks)

    def running_count(self) -> int:
        with self._lock:
            return sum(1 for task in self._tasks.values() if not task.done())


_STORE: Optional[JobStore] = None
_QUEUE: Optional[ResourceQueue] = None


def get_job_store() -> JobStore:
    global _STORE
    if _STORE is None:
        _STORE = JobStore()
    return _STORE


def get_job_queue() -> ResourceQueue:
    global _QUEUE
    if _QUEUE is None:
        _QUEUE = ResourceQueue(get_job_store())
    return _QUEUE