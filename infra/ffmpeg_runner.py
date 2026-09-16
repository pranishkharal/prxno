"""
Safe FFmpeg execution.

The previous implementation launched FFmpeg with ``subprocess.Popen`` and read
its output in a blocking loop, with only a timeout able to stop it. Nothing kept
track of live processes, so a cancelled job could not actually stop its encoder,
and success was inferred from the return code alone.

This layer adds:

* a registry of live processes, so one job - or the whole bot - can be cancelled
  deterministically,
* hard timeouts with real process termination (including the process tree on
  Windows via ``taskkill``),
* a captured stderr tail for diagnostics,
* return-code AND output validation (exists, non-empty, optionally decodable),
* optional verified hardware-encoder substitution with automatic CPU fallback.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

from .config import INFRA_CONFIG
from .hardware import CPU_ENCODER, HardwareProfile, cpu_thread_budget, substitute_hw_encoder
from .locking import probe_duration
from .logutil import EVENT_FFMPEG_COMPLETED, EVENT_FFMPEG_STARTED, log_event

_MAX_TAIL_LINES = 400


class FFmpegError(RuntimeError):
    """Base class for FFmpeg execution failures."""

    def __init__(self, message: str, returncode: Optional[int] = None, stderr_tail: str = ""):
        super().__init__(message)
        self.returncode = returncode
        self.stderr_tail = stderr_tail


class FFmpegTimeout(FFmpegError):
    """The encoder exceeded its time budget and was terminated."""


class FFmpegCancelled(FFmpegError):
    """The encoder was cancelled by a user or by shutdown."""


class FFmpegOutputError(FFmpegError):
    """FFmpeg reported success but the output is missing or unusable."""


@dataclass
class FFmpegResult:
    """Outcome of one FFmpeg invocation."""

    returncode: int
    command: List[str]
    elapsed_seconds: float
    encoder: str = CPU_ENCODER
    hardware_accelerated: bool = False
    stderr_tail: str = ""
    output_lines: List[str] = field(default_factory=list)


class _ProcessRegistry:
    """Thread-safe table of in-flight FFmpeg processes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._processes: Dict[str, subprocess.Popen] = {}
        self._timed_out = set()
        self._cancelled = set()

    def add(self, token: str, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes[token] = process

    def remove(self, token: str) -> None:
        with self._lock:
            self._processes.pop(token, None)

    def get(self, token: str):
        with self._lock:
            return self._processes.get(token)

    def tokens(self) -> List[str]:
        with self._lock:
            return list(self._processes)

    def count(self) -> int:
        with self._lock:
            return len(self._processes)

    def mark_timed_out(self, token: str) -> None:
        with self._lock:
            self._timed_out.add(token)

    def mark_cancelled(self, token: str) -> None:
        with self._lock:
            self._cancelled.add(token)

    def consume(self, token: str):
        with self._lock:
            timed_out = token in self._timed_out
            cancelled = token in self._cancelled
            self._timed_out.discard(token)
            self._cancelled.discard(token)
            return timed_out, cancelled

    def clear_flags(self, token: str) -> None:
        with self._lock:
            self._timed_out.discard(token)
            self._cancelled.discard(token)


def _terminate(process: subprocess.Popen, grace: float = 8.0) -> None:
    """Stop a process and its children as firmly as the OS allows."""
    if process is None or process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                check=False,
            )
        else:
            process.terminate()
    except Exception:
        pass
    try:
        process.wait(timeout=grace)
        return
    except Exception:
        pass
    try:
        process.kill()
        process.wait(timeout=grace)
    except Exception:
        pass


class FFmpegRunner:
    """Builds, runs, validates and cancels FFmpeg work."""

    def __init__(self, profile: Optional[HardwareProfile] = None, ffmpeg_path: Optional[str] = None):
        if profile is None:
            profile = HardwareProfile.detect(probe_encoders=False, use_cache=True)
        self.profile = profile
        self.ffmpeg_path = ffmpeg_path or profile.ffmpeg_path or "ffmpeg"
        self.registry = _ProcessRegistry()

    # ------------------------------------------------------------- planning
    def hardware_enabled(self) -> bool:
        """Honour the operator switch; never enable what is not verified."""
        mode = str(INFRA_CONFIG.auto_hw_encode or "false").strip().lower()
        if mode in ("0", "false", "no", "off", "disabled", ""):
            return False
        return bool(self.profile.has_hardware_encoder)

    def prepare(self, command: Sequence[str]) -> tuple:
        """Return (command, encoder, hardware_used)."""
        prepared = [str(part) for part in command]
        if not prepared:
            return prepared, CPU_ENCODER, False
        if self.hardware_enabled():
            encoder = self.profile.preferred_encoder
            rewritten, changed = substitute_hw_encoder(prepared, encoder)
            if changed:
                return rewritten, encoder, True
        capped, cpu_changed = self._cap_cpu_threads(prepared)
        if cpu_changed:
            prepared = capped
        return prepared, CPU_ENCODER, False

    def _cap_cpu_threads(self, command: List[str]) -> tuple:
        """Cap libx264/libx265 thread usage so one encode cannot starve downloads.

        Returns ``(command, changed)``. Hardware encodes and commands that
        already pin ``-threads`` are untouched, so the known-good paths stay
        intact.
        """
        try:
            video_index = command.index("-c:v")
        except ValueError:
            return command, False
        if video_index + 1 >= len(command):
            return command, False
        if command[video_index + 1] not in (CPU_ENCODER, "libx265", "libx264"):
            return command, False
        if "-threads" in command:
            return command, False
        budget = cpu_thread_budget(self.profile)
        if budget <= 0:
            return command, False
        return command + ["-threads", str(budget)], True

    # ------------------------------------------------------------ execution
    def run(
        self,
        command: Sequence[str],
        timeout: Optional[float] = None,
        job_id: Optional[str] = None,
        cancel_token: Optional[str] = None,
        outputs: Optional[Iterable] = None,
        check_duration: bool = False,
        allow_hardware: bool = True,
        echo: bool = True,
        env: Optional[Dict[str, str]] = None,
    ) -> FFmpegResult:
        """Run FFmpeg, raising a typed error on any failure."""
        timeout = float(timeout or INFRA_CONFIG.ffmpeg_timeout)
        prepared = [str(part) for part in command]
        encoder = CPU_ENCODER
        hardware = False

        if allow_hardware:
            prepared, encoder, hardware = self.prepare(prepared)
        elif prepared and "libx264" in prepared:
            encoder = CPU_ENCODER

        # Always call the runner\'s own ffmpeg when the caller used the bare name.
        if prepared and prepared[0] == "ffmpeg" and self.ffmpeg_path != "ffmpeg":
            prepared[0] = self.ffmpeg_path

        token = cancel_token or uuid.uuid4().hex[:12]
        self.registry.clear_flags(token)
        log_event(
            EVENT_FFMPEG_STARTED,
            job_id=job_id,
            encoder=encoder,
            hardware=hardware,
            command=" ".join(prepared),
        )

        started = time.time()
        process = subprocess.Popen(
            prepared,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            errors="replace",
            env=env,
        )
        self.registry.add(token, process)

        watchdog = threading.Timer(timeout, self._expire, args=(token, process))
        watchdog.daemon = True
        watchdog.start()

        output_lines: List[str] = []
        returncode = None
        try:
            stream = process.stdout
            if stream is not None:
                for line in stream:
                    line = line.rstrip()
                    if not line:
                        continue
                    output_lines.append(line)
                    if len(output_lines) > _MAX_TAIL_LINES:
                        output_lines.pop(0)
                    if echo:
                        print(line, flush=True)
            returncode = process.wait()
        except Exception as error:  # pragma: no cover - defensive
            _terminate(process)
            self.registry.remove(token)
            watchdog.cancel()
            timed_out, cancelled = self.registry.consume(token)
            if timed_out:
                raise FFmpegTimeout("FFmpeg timed out after %.0fs" % timeout)
            if cancelled:
                raise FFmpegCancelled("FFmpeg was cancelled")
            raise FFmpegError("FFmpeg execution error: %s" % error)
        finally:
            watchdog.cancel()
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except Exception:
                pass
            self.registry.remove(token)

        elapsed = time.time() - started
        tail = "\n".join(output_lines[-40:])
        timed_out, cancelled = self.registry.consume(token)

        if timed_out:
            raise FFmpegTimeout(
                "FFmpeg timed out after %.0fs" % timeout, returncode, tail
            )
        if cancelled:
            raise FFmpegCancelled("FFmpeg was cancelled", returncode, tail)
        if returncode not in (0, None):
            raise FFmpegError(
                "FFmpeg failed with exit code %s" % returncode, returncode, tail
            )

        if outputs:
            self._validate_outputs(outputs, check_duration=check_duration, tail=tail)

        log_event(
            EVENT_FFMPEG_COMPLETED,
            job_id=job_id,
            encoder=encoder,
            hardware=hardware,
            elapsed=round(elapsed, 2),
            returncode=returncode,
        )
        return FFmpegResult(
            returncode=returncode if returncode is not None else 0,
            command=prepared,
            elapsed_seconds=elapsed,
            encoder=encoder,
            hardware_accelerated=hardware,
            stderr_tail=tail,
            output_lines=output_lines,
        )

    def _expire(self, token: str, process) -> None:
        self.registry.mark_timed_out(token)
        _terminate(process)

    def _validate_outputs(self, outputs: Iterable, check_duration: bool, tail: str) -> None:
        for output in outputs:
            path = Path(output)
            if not path.exists():
                raise FFmpegOutputError("FFmpeg output was not created: %s" % path, None, tail)
            if not path.is_file() or path.stat().st_size <= 0:
                raise FFmpegOutputError("FFmpeg output is empty: %s" % path, None, tail)
            if check_duration and probe_duration(path) <= 0:
                raise FFmpegOutputError(
                    "FFmpeg output is not decodable: %s" % path, None, tail
                )

    # ---------------------------------------------------------- cancellation
    def cancel(self, token: str) -> bool:
        """Cancel one running FFmpeg by its token."""
        process = self.registry.get(token)
        if process is None:
            return False
        self.registry.mark_cancelled(token)
        _terminate(process)
        return True

    def cancel_all(self) -> int:
        """Cancel every running FFmpeg (used on shutdown)."""
        tokens = self.registry.tokens()
        for token in tokens:
            self.cancel(token)
        return len(tokens)

    def running(self) -> int:
        return self.registry.count()


_RUNNER: Optional[FFmpegRunner] = None


def get_ffmpeg_runner() -> FFmpegRunner:
    """Process-wide FFmpeg runner."""
    global _RUNNER
    if _RUNNER is None:
        _RUNNER = FFmpegRunner()
    return _RUNNER