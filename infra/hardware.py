"""
Runtime hardware detection and encoder selection.

The bot has to adapt to whatever machine it lands on: a weak laptop, a strong
desktop, or a GPU server. Nothing here assumes a GPU exists. Every hardware
encoder is verified with a real test encode before it is offered, so an
advertised but unusable encoder (missing driver, no device, no permission)
falls back to CPU automatically instead of failing jobs.
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .config import INFRA_CONFIG

CPU_ENCODER = "libx264"
HW_ENCODER_CANDIDATES = ("h264_nvenc", "h264_qsv", "h264_amf", "h264_vaapi")
_PROFILE_MAX_AGE_SECONDS = 7 * 24 * 3600

_PRESET_MAP = {
    CPU_ENCODER: {"fast": "veryfast", "balanced": "medium", "quality": "slow"},
    "h264_nvenc": {"fast": "p1", "balanced": "p4", "quality": "p6"},
    "h264_qsv": {"fast": "veryfast", "balanced": "medium", "quality": "slow"},
    "h264_amf": {"fast": "speed", "balanced": "balanced", "quality": "quality"},
    "h264_vaapi": {"fast": "fast", "balanced": "medium", "quality": "slow"},
}

_QUALITY_FROM_PRESET = {
    "ultrafast": "fast", "superfast": "fast", "veryfast": "fast",
    "faster": "fast", "fast": "fast", "medium": "balanced",
    "slow": "quality", "slower": "quality", "veryslow": "quality",
    "p1": "fast", "p2": "fast", "p3": "balanced", "p4": "balanced",
    "p5": "quality", "p6": "quality", "p7": "quality",
    "speed": "fast", "balanced": "balanced", "quality": "quality",
}


def _clamp(value, low, high):
    return max(low, min(high, value))


def _total_ram_bytes() -> Optional[int]:
    if os.name == "nt":
        try:
            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except Exception:
            return None
        return None
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def _available_ram_bytes() -> Optional[int]:
    if os.name == "nt":
        try:
            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullAvailPhys)
        except Exception:
            return None
        return None
    try:
        with open("/proc/meminfo", "r") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except Exception:
        return None
    return None


def _detect_gpu() -> Tuple[str, str]:
    """Return (vendor, name). Empty strings when nothing is detected."""
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            result = subprocess.run(
                [nvidia_smi, "--query-gpu=name", "--format=csv,noheader"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20,
            )
            name = (result.stdout or "").strip().splitlines()
            if result.returncode == 0 and name:
                return "nvidia", name[0].strip()
        except Exception:
            pass

    if os.name == "nt":
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "(Get-CimInstance Win32_VideoController | Select-Object -First 1 -ExpandProperty Name)"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=25,
            )
            name = (result.stdout or "").strip()
            if name:
                lowered = name.lower()
                vendor = "unknown"
                if "nvidia" in lowered or "geforce" in lowered or "quadro" in lowered:
                    vendor = "nvidia"
                elif "intel" in lowered or "iris" in lowered or "arc" in lowered or "uhd" in lowered:
                    vendor = "intel"
                elif "amd" in lowered or "radeon" in lowered:
                    vendor = "amd"
                return vendor, name
        except Exception:
            pass
    return "", ""


def _list_encoders(ffmpeg_path: str) -> List[str]:
    try:
        result = subprocess.run(
            [ffmpeg_path, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60,
        )
    except Exception:
        return []
    names: List[str] = []
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) == 6:
            candidate = parts[1]
            if candidate and candidate[0].isalpha():
                names.append(candidate)
    return names


def _verify_encoder(encoder: str, ffmpeg_path: str, ffprobe_path: str = "ffprobe") -> bool:
    """Prove the encoder really works on this machine.

    A zero exit code is not sufficient: some builds happily open an encoder and
    then emit nothing usable. The probe therefore encodes real frames to a real
    file and requires that file to exist, be non-empty, and decode back to at
    least one frame. Anything less and we fall back to CPU.
    """
    staging = Path(tempfile.mkdtemp(prefix="enccheck_"))
    target = staging / ("probe_%s.mp4" % encoder)
    command = [
        ffmpeg_path, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15:duration=1",
        "-c:v", encoder, "-pix_fmt", "yuv420p", str(target),
    ]
    try:
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=180,
        )
        if result.returncode != 0:
            return False
        if not target.exists() or target.stat().st_size <= 0:
            return False

        probe = subprocess.run(
            [ffprobe_path, "-v", "error", "-select_streams", "v:0",
             "-count_frames", "-show_entries", "stream=nb_read_frames",
             "-of", "csv=p=0", str(target)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=90,
        )
        frames = (probe.stdout or "").strip()
        return frames.isdigit() and int(frames) > 0
    except Exception:
        return False
    finally:
        try:
            if target.exists():
                target.unlink()
            staging.rmdir()
        except Exception:
            pass


def _resolve_tool(name: str) -> str:
    """Resolve an FFmpeg tool to an absolute path.

    A relative result would be stored in the cached hardware profile and would
    then break silently if the working directory ever changed.
    """
    found = shutil.which(name)
    if not found:
        return name
    try:
        return str(Path(found).resolve())
    except Exception:
        return found


@dataclass
class HardwareProfile:
    """A snapshot of what this machine can actually do."""

    cpu_count: int = 1
    cpu_count_physical: Optional[int] = None
    total_ram_gb: Optional[float] = None
    available_ram_gb: Optional[float] = None
    gpu_vendor: str = ""
    gpu_name: str = ""
    cuda_available: bool = False
    available_encoders: List[str] = field(default_factory=list)
    verified_encoders: List[str] = field(default_factory=list)
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    detected_at: float = field(default_factory=time.time)

    @property
    def preferred_encoder(self) -> str:
        for candidate in HW_ENCODER_CANDIDATES:
            if candidate in self.verified_encoders:
                return candidate
        return CPU_ENCODER

    @property
    def has_hardware_encoder(self) -> bool:
        return self.preferred_encoder != CPU_ENCODER

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "HardwareProfile":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Optional[Path] = None) -> None:
        target = Path(path) if path else INFRA_CONFIG.hw_profile_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        except Exception:
            pass

    @classmethod
    def load(cls, path: Optional[Path] = None, max_age: float = _PROFILE_MAX_AGE_SECONDS):
        source = Path(path) if path else INFRA_CONFIG.hw_profile_path
        try:
            if not source.exists():
                return None
            if max_age and (time.time() - source.stat().st_mtime) > max_age:
                return None
            return cls.from_dict(json.loads(source.read_text(encoding="utf-8")))
        except Exception:
            return None

    @classmethod
    def detect(cls, probe_encoders: bool = True, force: bool = False, use_cache: bool = True):
        if use_cache and not force:
            cached = cls.load()
            if cached is not None:
                return cached

        ffmpeg_path = _resolve_tool("ffmpeg")
        ffprobe_path = _resolve_tool("ffprobe")

        total_ram = _total_ram_bytes()
        avail_ram = _available_ram_bytes()
        vendor, name = _detect_gpu()

        cuda = False
        try:
            import torch  # noqa: WPS433 - optional and heavy, imported lazily
            cuda = bool(torch.cuda.is_available())
        except Exception:
            cuda = False

        physical = None
        try:
            import psutil  # noqa: WPS433 - optional
            physical = psutil.cpu_count(logical=False)
        except Exception:
            physical = None

        profile = cls(
            cpu_count=os.cpu_count() or 1,
            cpu_count_physical=physical,
            total_ram_gb=round(total_ram / (1024 ** 3), 2) if total_ram else None,
            available_ram_gb=round(avail_ram / (1024 ** 3), 2) if avail_ram else None,
            gpu_vendor=vendor,
            gpu_name=name,
            cuda_available=cuda,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            detected_at=time.time(),
        )

        if probe_encoders:
            available = _list_encoders(ffmpeg_path)
            profile.available_encoders = sorted(set(available))
            verified: List[str] = []
            for encoder in HW_ENCODER_CANDIDATES:
                if encoder in profile.available_encoders and _verify_encoder(
                    encoder, ffmpeg_path, ffprobe_path
                ):
                    verified.append(encoder)
            profile.verified_encoders = verified
            profile.save()

        return profile


def encoder_family(encoder: str) -> str:
    if encoder == CPU_ENCODER:
        return "cpu"
    if "nvenc" in encoder:
        return "nvidia"
    if "qsv" in encoder:
        return "intel"
    if "amf" in encoder:
        return "amd"
    if "vaapi" in encoder:
        return "vaapi"
    return "unknown"


def cpu_thread_budget(profile: Optional["HardwareProfile"] = None) -> int:
    """Threads one CPU encode may use without starving other work.

    ``libx264`` spawns frame threads on every visible core by default, so one
    encode can saturate the box and stall concurrent downloads. The budget is
    roughly half the usable cores (always reserving one for the event loop),
    clamped to ``[1, 8]``. Returns ``0`` when it cannot be determined or when
    a hardware encoder is preferred (GPU encodes do not need a CPU cap).
    """
    try:
        cores = int(getattr(profile, "cpu_count", 0) or os.cpu_count() or 0)
    except Exception:
        cores = 0
    if cores <= 0:
        return 0
    try:
        if getattr(profile, "has_hardware_encoder", False):
            return 0
    except Exception:
        pass
    usable = max(1, cores - (1 if cores > 2 else 0))
    return _clamp(max(1, usable // 2), 1, 8)


def build_video_encode_args(encoder: Optional[str] = None, quality: str = "balanced", crf: int = 20) -> List[str]:
    """Build the video-codec section of an FFmpeg command for ``encoder``."""
    encoder = encoder or CPU_ENCODER
    preset = _PRESET_MAP.get(encoder, _PRESET_MAP[CPU_ENCODER]).get(quality, "medium")
    crf = int(crf)
    if encoder == CPU_ENCODER:
        return ["-c:v", CPU_ENCODER, "-preset", preset, "-crf", str(crf)]
    if encoder in ("h264_nvenc", "hevc_nvenc", "av1_nvenc"):
        return ["-c:v", encoder, "-preset", preset, "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    if "qsv" in encoder:
        return ["-c:v", encoder, "-preset", preset, "-global_quality", str(crf)]
    if "amf" in encoder:
        return ["-c:v", encoder, "-quality", preset, "-qp_i", str(crf), "-qp_p", str(crf)]
    if "vaapi" in encoder:
        return ["-c:v", encoder, "-qp", str(crf)]
    return ["-c:v", CPU_ENCODER, "-preset", "medium", "-crf", str(crf)]


def substitute_hw_encoder(command: List[str], encoder: str) -> Tuple[List[str], bool]:
    """Rewrite a plain libx264 video encode inside ``command`` to ``encoder``.

    Returns ``(new_command, changed)``. Anything unexpected leaves the command
    untouched, so the existing known-good CPU path is always preserved.
    """
    if not encoder or encoder == CPU_ENCODER:
        return list(command), False

    cmd = list(command)
    try:
        index = cmd.index("-c:v")
    except ValueError:
        return cmd, False
    if index + 1 >= len(cmd) or cmd[index + 1] != CPU_ENCODER:
        return cmd, False

    quality = "balanced"
    crf = 20
    remove = {index, index + 1}
    cursor = index + 2
    while cursor + 1 < len(cmd) and cmd[cursor] in ("-preset", "-crf"):
        if cmd[cursor] == "-preset":
            quality = _QUALITY_FROM_PRESET.get(cmd[cursor + 1], quality)
        else:
            try:
                crf = int(float(cmd[cursor + 1]))
            except (TypeError, ValueError):
                pass
        remove.update({cursor, cursor + 1})
        cursor += 2

    rebuilt = [token for position, token in enumerate(cmd) if position not in remove]
    return rebuilt[:index] + build_video_encode_args(encoder, quality, crf) + rebuilt[index:], True