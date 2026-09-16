"""
Adaptive, resource-aware concurrency.

The previous implementation used fixed constants (10 edits, 5 downloads) on
every machine, and routed FFmpeg encoding *and* Whisper transcription through a
single shared semaphore. This module separates the three resource classes and
sizes each one from the detected hardware, while still honouring explicit
operator overrides.

The priority is stability, not throughput: it is always better to queue work
than to exhaust RAM or VRAM.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
from dataclasses import dataclass
from typing import Dict, Optional

from .config import INFRA_CONFIG
from .hardware import HardwareProfile

CLASS_JOB = "job"
CLASS_DOWNLOAD = "download"
CLASS_ANALYSIS = "analysis"
CLASS_ENCODE = "encode"
# ``CLASS_JOB`` is admission control for whole pipelines. It is deliberately
# roomy: the three resource classes below are the real throttles, and a tight
# job cap could deadlock a pipeline that still needs an encode slot.
ALL_CLASSES = (CLASS_JOB, CLASS_DOWNLOAD, CLASS_ANALYSIS, CLASS_ENCODE)


def _clamp(value, low, high):
    return max(low, min(high, value))


@dataclass(frozen=True)
class ResourceLimits:
    """Resolved concurrency budget per resource class."""

    jobs: int = 1
    downloads: int = 1
    analysis: int = 1
    encodes: int = 1
    reserved_cores: int = 0
    reason: str = ""

    def as_dict(self) -> Dict[str, int]:
        return {
            "jobs": self.jobs,
            "downloads": self.downloads,
            "analysis": self.analysis,
            "encodes": self.encodes,
            "reserved_cores": self.reserved_cores,
        }

    def for_class(self, name: str) -> int:
        if name == CLASS_JOB:
            return self.jobs
        if name == CLASS_DOWNLOAD:
            return self.downloads
        if name == CLASS_ANALYSIS:
            return self.analysis
        if name == CLASS_ENCODE:
            return self.encodes
        raise KeyError("unknown resource class: %s" % name)


def plan_limits(profile: Optional[HardwareProfile] = None) -> ResourceLimits:
    """Size each resource class from the hardware we actually have."""
    if profile is None:
        profile = HardwareProfile.detect(probe_encoders=False, use_cache=True)

    cores = max(1, int(profile.cpu_count or os.cpu_count() or 2))
    ram_gb = float(profile.total_ram_gb or 0.0)

    # Always leave a core for the event loop, Discord and file I/O.
    reserved = 1 if cores > 2 else 0
    usable = max(1, cores - reserved)

    # Downloads are network and disk bound, so they can safely outnumber cores.
    downloads = _clamp(usable if usable > 1 else 2, 2, 8)

    if profile.has_hardware_encoder:
        # The GPU does the heavy lifting, so several encodes may overlap, but
        # they stay bounded so RAM and VRAM can never be exhausted.
        encodes = _clamp(usable // 2, 2, 6)
    else:
        # Software x264 spawns its own frame threads, so the number of
        # concurrent encode *processes* is deliberately well below the core
        # count to avoid oversubscribing the CPU.
        encodes = _clamp(usable // 3, 1, 5)
        if ram_gb and ram_gb < 8:
            encodes = min(encodes, 2)

    # Whisper is the heaviest per-job consumer and scales poorly on CPU,
    # so it gets a deliberately small budget of its own.
    if profile.cuda_available:
        analysis = _clamp(usable // 2, 2, 3)
    elif ram_gb and ram_gb < 12:
        analysis = 1
    else:
        analysis = 1 if cores < 6 else 2

    # The two heavy classes together must never exceed the usable core budget.
    if encodes + analysis > usable:
        encodes = max(1, usable - analysis)

    # Whole pipelines are admitted generously; see the note on CLASS_JOB.
    jobs = downloads + analysis + encodes

    reason = profile.preferred_encoder if profile.has_hardware_encoder else "cpu"
    return ResourceLimits(
        jobs=jobs,
        downloads=downloads,
        analysis=analysis,
        encodes=encodes,
        reserved_cores=reserved,
        reason=reason,
    )


def _pick(explicit, env_value, base):
    if explicit:
        return int(explicit)
    if env_value:
        return int(env_value)
    return int(base)


class ResourceGovernor:
    """Owns one lazily created semaphore per resource class."""

    def __init__(self, profile=None, limits=None, overrides=None):
        if profile is None:
            profile = HardwareProfile.detect(probe_encoders=False, use_cache=True)
        self.profile = profile
        base = limits or plan_limits(profile)
        overrides = overrides or {}
        self.limits = ResourceLimits(
            jobs=base.jobs,
            downloads=_pick(overrides.get("downloads"), INFRA_CONFIG.max_concurrent_downloads, base.downloads),
            analysis=_pick(overrides.get("analysis"), INFRA_CONFIG.max_concurrent_analysis, base.analysis),
            encodes=_pick(overrides.get("encodes"), INFRA_CONFIG.max_concurrent_edits, base.encodes),
            reserved_cores=base.reserved_cores,
            reason=base.reason,
        )
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        # Downloads are network/disk bound and must never starve behind heavy
        # CPU work: they get a small dedicated thread pool so an edit hogging
        # the default executor cannot stall a concurrent !download.
        self._executors: Dict[str, concurrent.futures.ThreadPoolExecutor] = {}

    def executor(self, resource_class: str) -> Optional[concurrent.futures.Executor]:
        """Return a dedicated executor for ``resource_class`` (or None).

        Only the download class owns one today. Returning None keeps the
        default ``asyncio.to_thread`` behaviour for everything else.
        """
        if resource_class != CLASS_DOWNLOAD:
            return None
        pool = self._executors.get(resource_class)
        if pool is None:
            workers = max(2, self.limit(resource_class))
            pool = concurrent.futures.ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="dl",
            )
            self._executors[resource_class] = pool
        return pool

    async def run_in_class(self, resource_class: str, func, *args):
        """Run ``func`` under the class semaphore and its executor."""
        async with self.semaphore(resource_class):
            loop = asyncio.get_running_loop()
            pool = self.executor(resource_class)
            if pool is None:
                return await asyncio.to_thread(func, *args)
            return await loop.run_in_executor(pool, func, *args)

    def semaphore(self, resource_class: str) -> asyncio.Semaphore:
        semaphore = self._semaphores.get(resource_class)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.limit(resource_class))
            self._semaphores[resource_class] = semaphore
        return semaphore

    def limit(self, resource_class: str) -> int:
        return self.limits.for_class(resource_class)

    def describe(self) -> Dict[str, object]:
        return {
            "limits": self.limits.as_dict(),
            "reason": self.limits.reason,
            "cpu_count": self.profile.cpu_count,
            "total_ram_gb": self.profile.total_ram_gb,
            "gpu": self.profile.gpu_name or "none",
            "cuda": self.profile.cuda_available,
            "preferred_encoder": self.profile.preferred_encoder,
        }

    def log_summary(self) -> Dict[str, object]:
        from .logutil import log_event
        summary = self.describe()
        log_event("HARDWARE_PROFILE", **summary)
        return summary


_GOVERNOR: Optional[ResourceGovernor] = None


def get_governor() -> ResourceGovernor:
    """Return the process-wide governor, creating it on first use."""
    global _GOVERNOR
    if _GOVERNOR is None:
        _GOVERNOR = ResourceGovernor()
    return _GOVERNOR


def reset_governor() -> None:
    """Drop the cached governor (used by tests and configuration reloads)."""
    global _GOVERNOR
    _GOVERNOR = None