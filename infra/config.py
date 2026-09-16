"""
Environment-driven configuration for the infrastructure layer.

Every tunable has a safe default so the bot keeps working with an empty .env.
Concurrency defaults to automatic hardware detection; the environment
variables below exist only as explicit operator overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(float(raw.strip()))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _env_optional_int(name: str) -> Optional[int]:
    """Return None when unset, so adaptive detection can take over."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        value = int(float(raw.strip()))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


@dataclass
class InfraConfig:
    """Resolved infrastructure configuration."""

    project_root: Path = PROJECT_ROOT

    cache_root: Path = field(default_factory=lambda: PROJECT_ROOT / "cache")
    job_root: Path = field(default_factory=lambda: PROJECT_ROOT / "temp" / "jobs")
    log_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "logs")

    cache_ttl_seconds: int = 86400
    temp_max_age_seconds: int = 21600
    source_min_bytes: int = 10000
    source_min_duration: float = 0.5
    source_max_duration: float = 14400.0

    cache_lock_timeout: float = 900.0
    ffmpeg_timeout: int = 1800
    ffprobe_timeout: int = 60
    job_max_attempts: int = 3
    job_retry_backoff: float = 2.0

    max_concurrent_downloads: Optional[int] = None
    max_concurrent_analysis: Optional[int] = None
    max_concurrent_edits: Optional[int] = None

    auto_hw_encode: str = "false"

    jsonl_logging: bool = True
    console_logging: bool = True

    @property
    def source_cache_dir(self) -> Path:
        return self.cache_root / "sources"

    @property
    def analysis_cache_dir(self) -> Path:
        return self.cache_root / "analysis"

    @property
    def hw_profile_path(self) -> Path:
        return self.cache_root / "hardware.json"

    def ensure_dirs(self) -> None:
        for path in (
            self.cache_root,
            self.source_cache_dir,
            self.analysis_cache_dir,
            self.job_root,
            self.log_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "InfraConfig":
        return cls(
            cache_root=Path(_env_str("CACHE_ROOT", str(PROJECT_ROOT / "cache"))),
            job_root=Path(_env_str("JOB_ROOT", str(PROJECT_ROOT / "temp" / "jobs"))),
            log_dir=Path(_env_str("LOG_DIR", str(PROJECT_ROOT / "logs"))),
            cache_ttl_seconds=_env_int("CACHE_TTL", 86400),
            temp_max_age_seconds=_env_int("TEMP_MAX_AGE", 21600),
            source_min_bytes=_env_int("SOURCE_MIN_BYTES", 10000),
            cache_lock_timeout=_env_float("CACHE_LOCK_TIMEOUT", 900.0),
            ffmpeg_timeout=_env_int("FFMPEG_TIMEOUT", 1800),
            job_max_attempts=_env_int("JOB_MAX_ATTEMPTS", 3),
            job_retry_backoff=_env_float("JOB_RETRY_BACKOFF", 2.0),
            max_concurrent_downloads=_env_optional_int("MAX_CONCURRENT_DOWNLOADS"),
            max_concurrent_analysis=_env_optional_int("MAX_CONCURRENT_ANALYSIS"),
            max_concurrent_edits=_env_optional_int("MAX_CONCURRENT_EDITS"),
            auto_hw_encode=_env_str("AUTO_HW_ENCODE", "false").lower(),
            jsonl_logging=_env_bool("INFRA_JSONL_LOG", True),
            console_logging=_env_bool("INFRA_CONSOLE_LOG", True),
        )


INFRA_CONFIG = InfraConfig.from_env()


def reload_config() -> InfraConfig:
    """Re-read the environment. Used by tests and by long-running reloads."""
    global INFRA_CONFIG
    INFRA_CONFIG = InfraConfig.from_env()
    return INFRA_CONFIG