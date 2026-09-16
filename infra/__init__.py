"""Infrastructure layer for Auto Clips for Kick.

Production-grade plumbing used by the bot:

- structured, job-correlated logging          (infra.logutil)
- hardware detection + encoder selection      (infra.hardware)
- adaptive, resource-aware concurrency        (infra.concurrency)
- atomic file operations and locking          (infra.locking)
- a deduplicating source cache                (infra.source_cache)
- a versioned analysis / transcript cache     (infra.analysis_cache)
- isolated per-job workspaces                 (infra.workspace)
- a safe FFmpeg execution layer               (infra.ffmpeg_runner)
- a shared Whisper model manager              (infra.whisper_manager)
- a durable job store and queue               (infra.jobs)

Submodules are imported directly (e.g. ``from infra.source_cache import
SourceCache``) so this package never introduces an import cycle into the
existing application modules.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]