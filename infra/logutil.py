"""
Structured, job-correlated logging for the infrastructure layer.

Two sinks are supported:

* a human readable console line, so operators keep seeing progress,
* a JSON-lines file (``logs/infra.jsonl``) for machine analysis.

Secrets are never written: token-shaped strings are replaced and any field
whose name looks like a credential is redacted outright.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import INFRA_CONFIG

EVENT_JOB_CREATED = "JOB_CREATED"
EVENT_JOB_QUEUED = "JOB_QUEUED"
EVENT_JOB_STARTED = "JOB_STARTED"
EVENT_DOWNLOAD_STARTED = "DOWNLOAD_STARTED"
EVENT_DOWNLOAD_CACHE_HIT = "DOWNLOAD_CACHE_HIT"
EVENT_DOWNLOAD_COMPLETED = "DOWNLOAD_COMPLETED"
EVENT_ANALYSIS_STARTED = "ANALYSIS_STARTED"
EVENT_ANALYSIS_CACHE_HIT = "ANALYSIS_CACHE_HIT"
EVENT_ANALYSIS_COMPLETED = "ANALYSIS_COMPLETED"
EVENT_EDIT_STARTED = "EDIT_STARTED"
EVENT_FFMPEG_STARTED = "FFMPEG_STARTED"
EVENT_FFMPEG_COMPLETED = "FFMPEG_COMPLETED"
EVENT_JOB_COMPLETED = "JOB_COMPLETED"
EVENT_JOB_FAILED = "JOB_FAILED"
EVENT_JOB_CANCELLED = "JOB_CANCELLED"
EVENT_CLEANUP_COMPLETED = "CLEANUP_COMPLETED"

ALL_EVENTS = (
    EVENT_JOB_CREATED, EVENT_JOB_QUEUED, EVENT_JOB_STARTED,
    EVENT_DOWNLOAD_STARTED, EVENT_DOWNLOAD_CACHE_HIT, EVENT_DOWNLOAD_COMPLETED,
    EVENT_ANALYSIS_STARTED, EVENT_ANALYSIS_CACHE_HIT, EVENT_ANALYSIS_COMPLETED,
    EVENT_EDIT_STARTED, EVENT_FFMPEG_STARTED, EVENT_FFMPEG_COMPLETED,
    EVENT_JOB_COMPLETED, EVENT_JOB_FAILED, EVENT_JOB_CANCELLED,
    EVENT_CLEANUP_COMPLETED,
)

_SECRET_FIELD_HINTS = (
    "token", "secret", "password", "passwd", "api_key", "apikey",
    "authorization", "cookie", "credential", "private_key",
)

# Matches Discord-style tokens and ``Bot <token>`` authorization headers.
_TOKEN_PATTERN = re.compile(
    r"(?:Bot\s+)?[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}"
)

_LOCK = threading.Lock()
_MAX_VALUE_LEN = 300


def redact(value: Any) -> Any:
    """Replace token-shaped content inside a string."""
    if not isinstance(value, str):
        return value
    return _TOKEN_PATTERN.sub("<redacted>", value)


def _sanitize(fields: Dict[str, Any]) -> Dict[str, Any]:
    clean: Dict[str, Any] = {}
    for key, value in fields.items():
        name = str(key)
        if any(hint in name.lower() for hint in _SECRET_FIELD_HINTS):
            clean[name] = "<redacted>"
        elif isinstance(value, str):
            clean[name] = redact(value)
        elif isinstance(value, Path):
            clean[name] = str(value)
        else:
            clean[name] = value
    return clean


class StructuredLogger:
    """Writes structured events to a JSONL file and/or the console."""

    def __init__(
        self,
        log_dir: Optional[Path] = None,
        jsonl: Optional[bool] = None,
        console: Optional[bool] = None,
        filename: str = "infra.jsonl",
    ):
        self.log_dir = Path(log_dir) if log_dir else Path(INFRA_CONFIG.log_dir)
        self.jsonl_enabled = INFRA_CONFIG.jsonl_logging if jsonl is None else bool(jsonl)
        self.console_enabled = INFRA_CONFIG.console_logging if console is None else bool(console)
        self.path = self.log_dir / filename
        if self.jsonl_enabled:
            try:
                self.log_dir.mkdir(parents=True, exist_ok=True)
            except OSError:
                self.jsonl_enabled = False

    def log(self, event: str, job_id: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        record: Dict[str, Any] = {"ts": round(time.time(), 3), "event": event}
        if job_id:
            record["job_id"] = job_id
        record.update(_sanitize(fields))
        if self.jsonl_enabled:
            self._write(record)
        if self.console_enabled:
            self._console(record)
        return record

    def _write(self, record: Dict[str, Any]) -> None:
        try:
            line = json.dumps(record, ensure_ascii=False, default=str)
            with _LOCK:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception:
            # Logging must never take the bot down.
            self.jsonl_enabled = False

    def _console(self, record: Dict[str, Any]) -> None:
        parts = ["[%s]" % record["event"]]
        if record.get("job_id"):
            parts.append("job=%s" % record["job_id"])
        for key, value in record.items():
            if key in ("ts", "event", "job_id"):
                continue
            text = str(value)
            if len(text) > _MAX_VALUE_LEN:
                text = text[:_MAX_VALUE_LEN] + "..."
            parts.append("%s=%s" % (key, text))
        try:
            print(" ".join(parts), flush=True)
        except Exception:
            pass


LOGGER = StructuredLogger()


def log_event(event: str, job_id: Optional[str] = None, **fields: Any):
    """Log one infrastructure event through the shared logger."""
    return LOGGER.log(event, job_id=job_id, **fields)