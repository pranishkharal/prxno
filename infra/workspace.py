"""
Isolated per-job workspaces.

Every job gets its own directory tree, so two jobs can never share a mutable
temporary filename:

    <job_root>/<job_id>/input
    <job_root>/<job_id>/work
    <job_root>/<job_id>/output

Cleanup is scoped to that tree only. A job never removes a shared cache entry,
because the source cache owns those files.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Iterable, List, Optional

from .config import INFRA_CONFIG
from .locking import safe_rmtree
from .logutil import EVENT_CLEANUP_COMPLETED, log_event


class JobWorkspace:
    """A private, disposable directory tree for one job."""

    def __init__(self, job_id: str, root: Optional[Path] = None):
        self.job_id = str(job_id)
        self.root = Path(root) if root else Path(INFRA_CONFIG.job_root)
        self.path = self.root / self.job_id
        self.input_dir = self.path / "input"
        self.work_dir = self.path / "work"
        self.output_dir = self.path / "output"

    @classmethod
    def create(cls, job_id: Optional[str] = None, root: Optional[Path] = None) -> "JobWorkspace":
        workspace = cls(job_id or uuid.uuid4().hex[:12], root=root)
        return workspace.ensure()

    def ensure(self) -> "JobWorkspace":
        for directory in (self.input_dir, self.work_dir, self.output_dir):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    def input_path(self, name: str) -> Path:
        return self.input_dir / name

    def work_path(self, name: str) -> Path:
        return self.work_dir / name

    def output_path(self, name: str) -> Path:
        return self.output_dir / name

    def exists(self) -> bool:
        return self.path.exists()

    def cleanup(self, keep_output: bool = False, keep_input: bool = False) -> List[str]:
        """Remove this job tree. Never touches anything outside ``path``."""
        removed: List[str] = []
        if not keep_output and self.output_dir.exists() and safe_rmtree(self.output_dir):
            removed.append("output")
        if not keep_input and self.input_dir.exists() and safe_rmtree(self.input_dir):
            removed.append("input")
        if self.work_dir.exists() and safe_rmtree(self.work_dir):
            removed.append("work")
        try:
            self.path.rmdir()
        except OSError:
            pass
        log_event(
            EVENT_CLEANUP_COMPLETED,
            job_id=self.job_id,
            scope="workspace",
            removed=",".join(removed),
        )
        return removed

    def __enter__(self) -> "JobWorkspace":
        return self.ensure()

    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        self.cleanup()
        return False


def sweep_stale_workspaces(
    root: Optional[Path] = None,
    max_age_seconds: Optional[float] = None,
    active_job_ids: Optional[Iterable[str]] = None,
) -> List[str]:
    """Delete job directories older than the configured age.

    Job ids in ``active_job_ids`` are always skipped, so a slow job can never
    have its workspace removed by the sweeper.
    """
    base = Path(root) if root else Path(INFRA_CONFIG.job_root)
    max_age = INFRA_CONFIG.temp_max_age_seconds if max_age_seconds is None else max_age_seconds
    active = set(active_job_ids or ())
    removed: List[str] = []
    if not base.exists():
        return removed

    now = time.time()
    for entry in base.iterdir():
        try:
            if not entry.is_dir() or entry.name in active:
                continue
            if (now - entry.stat().st_mtime) > max_age and safe_rmtree(entry):
                removed.append(entry.name)
        except OSError:
            continue
    return removed