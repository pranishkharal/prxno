"""
VOD Job Manager - Handles the complete lifecycle of VOD analysis jobs.

Job States:
    CREATED -> DOWNLOADING -> DOWNLOAD_VALIDATED -> MEDIA_PROBED ->
    TRANSCRIBING -> ANALYZING -> CANDIDATES_FOUND -> RANKING ->
    EDITING -> ENCODING -> VALIDATING -> READY -> UPLOADING -> CLEANUP -> COMPLETE

Failure States:
    Any state -> FAILED -> RECOVERY/CLEANUP
"""

import asyncio
import json
import time
import uuid
import traceback
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
import threading

from .config import CONFIG, JobConfig, ContentType


class JobState(str, Enum):
    CREATED = "CREATED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOAD_VALIDATED = "DOWNLOAD_VALIDATED"
    MEDIA_PROBED = "MEDIA_PROBED"
    TRANSCRIBING = "TRANSCRIBING"
    ANALYZING = "ANALYZING"
    CANDIDATES_FOUND = "CANDIDATES_FOUND"
    RANKING = "RANKING"
    EDITING = "EDITING"
    ENCODING = "ENCODING"
    VALIDATING = "VALIDATING"
    READY = "READY"
    UPLOADING = "UPLOADING"
    CLEANUP = "CLEANUP"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class JobErrorType(str, Enum):
    DOWNLOAD_ERROR = "DOWNLOAD_ERROR"
    MEDIA_ERROR = "MEDIA_ERROR"
    TRANSCRIPTION_ERROR = "TRANSCRIPTION_ERROR"
    ANALYSIS_ERROR = "ANALYSIS_ERROR"
    FFMPEG_ERROR = "FFMPEG_ERROR"
    FILE_LOCK_ERROR = "FILE_LOCK_ERROR"
    TIMEOUT_ERROR = "TIMEOUT_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    CLEANUP_ERROR = "CLEANUP_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


@dataclass
class JobError:
    error_type: JobErrorType
    message: str
    details: str = ""
    timestamp: float = field(default_factory=time.time)
    recoverable: bool = True

    def to_dict(self) -> dict:
        return {
            "error_type": self.error_type.value,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp,
            "recoverable": self.recoverable,
        }


@dataclass
class ClipCandidate:
    """A candidate clip ready for review/editing."""
    id: str
    start_time: float
    end_time: float
    duration: float
    score: float
    signals: Dict[str, float]
    hook: str
    context_summary: str
    payoff: str
    reaction: str
    story_quality: float
    transcript_excerpt: str
    suggested_edit_style: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ClipCandidate":
        return cls(**data)


@dataclass
class VODJob:
    """Represents a single content analysis job (VOD, Clip, or Live)."""
    id: str
    vod_url: str
    user_id: int
    channel_id: int
    content_type: ContentType = ContentType.VOD
    state: JobState = JobState.CREATED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[JobError] = None
    progress: float = 0.0
    current_step: str = ""
    vod_path: Optional[str] = None
    vod_duration: float = 0.0
    vod_title: str = ""
    streamer_name: str = ""
    transcript_path: Optional[str] = None
    analysis_path: Optional[str] = None
    candidates: List[ClipCandidate] = field(default_factory=list)
    selected_clips: List[str] = field(default_factory=list)
    output_files: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = asdict(self)
        data["state"] = self.state.value
        data["content_type"] = self.content_type.value
        data["error"] = self.error.to_dict() if self.error else None
        data["candidates"] = [c.to_dict() for c in self.candidates]
        return data

    def select_clip(self, candidate_id: str) -> bool:
        if candidate_id not in self.selected_clips:
            self.selected_clips.append(candidate_id)
            return True
        return False

    def deselect_clip(self, candidate_id: str) -> bool:
        if candidate_id in self.selected_clips:
            self.selected_clips.remove(candidate_id)
            return True
        return False

    @classmethod
    def from_dict(cls, data: dict) -> "VODJob":
        data = data.copy()
        data["state"] = JobState(data["state"])
        data["content_type"] = ContentType(data.get("content_type", "vod"))
        if data["error"]:
            data["error"] = JobError(**data["error"])
        data["candidates"] = [ClipCandidate.from_dict(c) for c in data["candidates"]]
        return cls(**data)


class JobManager:
    """
    Manages VOD job lifecycle with persistence, concurrency control, and recovery.
    """

    def __init__(self, config: JobConfig = None):
        self.config = config or CONFIG.job
        self.jobs_dir = CONFIG.paths.jobs_dir
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: Dict[str, VODJob] = {}
        self._lock = threading.RLock()
        self._callbacks: Dict[str, List[Callable]] = {}
        self._running_jobs: Dict[str, asyncio.Task] = {}
        self._load_jobs()

    def _load_jobs(self):
        """Load persisted jobs from disk."""
        for job_file in self.jobs_dir.glob("*.json"):
            try:
                with open(job_file, 'r') as f:
                    data = json.load(f)
                job = VODJob.from_dict(data)
                self._jobs[job.id] = job
            except Exception as e:
                print(f"Failed to load job {job_file}: {e}")

    def _save_job(self, job: VODJob):
        """Persist job to disk."""
        job.updated_at = time.time()
        job_file = self.jobs_dir / f"{job.id}.json"
        try:
            with open(job_file, 'w') as f:
                json.dump(job.to_dict(), f, indent=2)
        except Exception as e:
            print(f"Failed to save job {job.id}: {e}")

    def create_job(self, vod_url: str, user_id: int, channel_id: int, content_type: ContentType = ContentType.VOD) -> VODJob:
        """Create a new content analysis job."""
        job_id = str(uuid.uuid4())[:8]
        job = VODJob(
            id=job_id,
            vod_url=vod_url,
            user_id=user_id,
            channel_id=channel_id,
            content_type=content_type,
        )
        with self._lock:
            self._jobs[job_id] = job
            self._save_job(job)
        return job

    def get_job(self, job_id: str) -> Optional[VODJob]:
        """Get job by ID."""
        with self._lock:
            return self._jobs.get(job_id)

    def get_user_jobs(self, user_id: int) -> List[VODJob]:
        """Get all jobs for a user."""
        with self._lock:
            return [j for j in self._jobs.values() if j.user_id == user_id]

    def get_active_jobs(self) -> List[VODJob]:
        """Get all non-terminal jobs."""
        with self._lock:
            return [
                j for j in self._jobs.values()
                if j.state not in (JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED)
            ]

    def update_state(self, job_id: str, state: JobState, progress: float = None, step: str = None):
        """Update job state with optional progress and step description."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.state = state
            if progress is not None:
                job.progress = max(0.0, min(100.0, progress))
            if step:
                job.current_step = step
            if state == JobState.DOWNLOADING and job.started_at is None:
                job.started_at = time.time()
            if state in (JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED):
                job.completed_at = time.time()
            self._save_job(job)
            self._notify(job_id, "state_changed", {"state": state.value, "progress": job.progress})
            return True

    def set_error(self, job_id: str, error_type: JobErrorType, message: str, details: str = "", recoverable: bool = True):
        """Set job error and transition to FAILED state."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.error = JobError(error_type=error_type, message=message, details=details, recoverable=recoverable)
            job.state = JobState.FAILED
            job.completed_at = time.time()
            self._save_job(job)
            self._notify(job_id, "error", job.error.to_dict())
            return True

    def update_progress(self, job_id: str, progress: float, step: str = None):
        """Update job progress percentage."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.progress = max(0.0, min(100.0, progress))
            if step:
                job.current_step = step
            self._save_job(job)
            self._notify(job_id, "progress", {"progress": job.progress, "step": job.current_step})
            return True

    def set_vod_info(self, job_id: str, path: str, duration: float, title: str, streamer: str):
        """Set VOD file info after download."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            job.vod_path = path
            job.vod_duration = duration
            job.vod_title = title
            job.streamer_name = streamer
            self._save_job(job)
            return True

    def set_transcript_path(self, job_id: str, path: str):
        """Set transcript file path."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.transcript_path = path
                self._save_job(job)
                return True
            return False

    def set_analysis_path(self, job_id: str, path: str):
        """Set analysis results file path."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.analysis_path = path
                self._save_job(job)
                return True
            return False

    def set_candidates(self, job_id: str, candidates: List[ClipCandidate]):
        """Set candidate clips."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                job.candidates = candidates
                self._save_job(job)
                self._notify(job_id, "candidates_ready", {"count": len(candidates)})
                return True
            return False

    def select_clip(self, job_id: str, candidate_id: str) -> bool:
        """Mark a candidate as selected for editing."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job and candidate_id not in job.selected_clips:
                job.selected_clips.append(candidate_id)
                self._save_job(job)
                return True
            return False

    def deselect_clip(self, job_id: str, candidate_id: str) -> bool:
        """Unmark a candidate."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job and candidate_id in job.selected_clips:
                job.selected_clips.remove(candidate_id)
                self._save_job(job)
                return True
            return False

    def add_output_file(self, job_id: str, output_path: str):
        """Add an output file path."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job and output_path not in job.output_files:
                job.output_files.append(output_path)
                self._save_job(job)
                return True
            return False

    def cancel_job(self, job_id: str) -> bool:
        """Cancel a running job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.state in (JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED):
                return False
            job.state = JobState.CANCELLED
            job.completed_at = time.time()
            self._save_job(job)
            # Cancel running task if exists
            task = self._running_jobs.pop(job_id, None)
            if task and not task.done():
                task.cancel()
            self._notify(job_id, "cancelled", {})
            return True

    def cleanup_job(self, job_id: str) -> bool:
        """Clean up job files and remove from memory."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            # Delete VOD file
            if job.vod_path:
                try:
                    Path(job.vod_path).unlink(missing_ok=True)
                except Exception:
                    pass
            # Delete transcript
            if job.transcript_path:
                try:
                    Path(job.transcript_path).unlink(missing_ok=True)
                except Exception:
                    pass
            # Delete analysis
            if job.analysis_path:
                try:
                    Path(job.analysis_path).unlink(missing_ok=True)
                except Exception:
                    pass
            # Delete output files
            for out_path in job.output_files:
                try:
                    Path(out_path).unlink(missing_ok=True)
                except Exception:
                    pass
            # Delete job file
            job_file = self.jobs_dir / f"{job_id}.json"
            try:
                job_file.unlink(missing_ok=True)
            except Exception:
                pass
            del self._jobs[job_id]
            return True

    def register_callback(self, job_id: str, callback: Callable):
        """Register a callback for job events."""
        if job_id not in self._callbacks:
            self._callbacks[job_id] = []
        self._callbacks[job_id].append(callback)

    def _notify(self, job_id: str, event: str, data: dict):
        """Notify callbacks of job events."""
        callbacks = self._callbacks.get(job_id, [])
        for cb in callbacks:
            try:
                cb(event, data)
            except Exception as e:
                print(f"Callback error for job {job_id}: {e}")

    async def run_job(self, job_id: str, pipeline_func: Callable):
        """Run a job with the given pipeline function."""
        job = self.get_job(job_id)
        if not job:
            return
        if job.state not in (JobState.CREATED, JobState.FAILED):
            return

        # Reset for retry
        job.state = JobState.CREATED
        job.error = None
        job.progress = 0.0
        job.started_at = None
        job.completed_at = None
        self._save_job(job)

        task = asyncio.create_task(self._execute_pipeline(job_id, pipeline_func))
        self._running_jobs[job_id] = task
        try:
            await task
        except asyncio.CancelledError:
            self.update_state(job_id, JobState.CANCELLED)
        except Exception as e:
            traceback.print_exc()
            self.set_error(job_id, JobErrorType.UNKNOWN_ERROR, str(e), traceback.format_exc())
        finally:
            self._running_jobs.pop(job_id, None)

    async def _execute_pipeline(self, job_id: str, pipeline_func: Callable):
        """Execute the pipeline function with timeout handling."""
        try:
            await asyncio.wait_for(
                pipeline_func(job_id, self),
                timeout=self.config.analysis_timeout_seconds
            )
        except asyncio.TimeoutError:
            self.set_error(job_id, JobErrorType.TIMEOUT_ERROR, "Job timed out", recoverable=True)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            raise


# Global job manager instance
_job_manager: Optional[JobManager] = None


def get_job_manager() -> JobManager:
    global _job_manager
    if _job_manager is None:
        _job_manager = JobManager()
    return _job_manager