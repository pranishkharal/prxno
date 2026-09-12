"""
VOD Downloader - Downloads full KICK VODs using yt-dlp.

Handles:
- Full VOD URLs (not just clips)
- Resume capability for large files
- Progress tracking
- Format selection for best quality
- Validation of downloaded file
"""

import asyncio
import shutil
import subprocess
import tempfile
import traceback
from pathlib import Path
from typing import Optional, Tuple, Callable
import yt_dlp

from .config import CONFIG
from .job_manager import JobManager, JobState, JobErrorType


class VODDownloadError(Exception):
    """Custom exception for VOD download errors."""
    def __init__(self, message: str, error_type: JobErrorType = JobErrorType.DOWNLOAD_ERROR, recoverable: bool = True):
        super().__init__(message)
        self.error_type = error_type
        self.recoverable = recoverable


async def download_kick_vod(
    url: str,
    job_id: str,
    job_manager: JobManager,
    progress_callback: Optional[Callable[[float, str], None]] = None
) -> Tuple[Path, float, str, str]:
    """
    Download a KICK VOD to local storage.

    Args:
        url: KICK VOD URL
        job_id: Job ID for tracking
        job_manager: Job manager for state updates
        progress_callback: Optional callback(progress_percent, step_description)

    Returns:
        Tuple of (video_path, duration_seconds, title, streamer_name)

    Raises:
        VODDownloadError: On download failure
    """

    def _report(progress: float, step: str):
        job_manager.update_progress(job_id, progress, step)
        if progress_callback:
            progress_callback(progress, step)

    # Validate URL
    if not is_kick_vod_url(url):
        raise VODDownloadError(
            "Not a valid KICK VOD URL. Expected format: https://kick.com/username/videos/...",
            JobErrorType.DOWNLOAD_ERROR,
            recoverable=False
        )

    # Extract streamer name from URL
    streamer_name = extract_streamer_from_vod_url(url) or "unknown"

    # Create job-specific directory
    job_dir = CONFIG.paths.downloads_dir / f"vod_{job_id}"
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(job_dir / "vod_%(id)s.%(ext)s")

    # yt-dlp options for VOD download
    ydl_opts = {
        "outtmpl": output_template,
        "format": "best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "quiet": False,
        "no_warnings": False,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 120,
        "http_chunk_size": 10485760,  # 10MB chunks
        "concurrent_fragment_downloads": 4,
        "progress_hooks": [_make_progress_hook(job_manager, job_id, _report)],
    }

    # Add impersonation for KICK
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        ydl_opts["impersonate"] = ImpersonateTarget.from_str("chrome")
    except Exception as e:
        print(f"Impersonation unavailable: {e}")

    print(f"\n========================================")
    print(f" DOWNLOADING KICK VOD")
    print(f"========================================")
    print(f"URL: {url}")
    print(f"Job: {job_id}")
    print(f"Output dir: {job_dir}")

    job_manager.update_state(job_id, JobState.DOWNLOADING, 0, "Starting download...")

    try:
        # Run yt-dlp in thread pool
        loop = asyncio.get_event_loop()
        info = await loop.run_in_executor(None, _download_with_ytdlp, url, ydl_opts)

        if not info:
            raise VODDownloadError(
                "yt-dlp could not download this VOD. KICK VOD downloads may not be supported yet. "
                "Try using a clip URL instead: https://kick.com/username/clips/12345678",
                JobErrorType.DOWNLOAD_ERROR,
                recoverable=False
            )

        # Check if download actually produced a file
        candidates = list(job_dir.glob("*"))
        video_candidates = [
            p for p in candidates
            if p.is_file()
            and p.suffix.lower() in {".mp4", ".mov", ".mkv", ".webm"}
        ]
        if not video_candidates:
            raise VODDownloadError(
                "yt-dlp could not download this VOD. KICK VOD downloads may not be supported yet. "
                "Try using a clip URL instead: https://kick.com/username/clips/12345678",
                JobErrorType.DOWNLOAD_ERROR,
                recoverable=False
            )

        # Find downloaded file
        video_file = _find_downloaded_video(job_dir, info)
        if not video_file:
            raise VODDownloadError("Download completed but no video file found", JobErrorType.DOWNLOAD_ERROR)

        # Validate file
        if video_file.stat().st_size < 100_000:
            raise VODDownloadError("Downloaded file is too small", JobErrorType.MEDIA_ERROR)

        # Get duration and title
        duration = get_video_duration(video_file)
        title = info.get("title", f"VOD_{streamer_name}")

        if duration <= 0:
            raise VODDownloadError("Invalid video duration", JobErrorType.MEDIA_ERROR)

        if duration > CONFIG.job.max_vod_duration_hours * 3600:
            raise VODDownloadError(
                f"VOD exceeds maximum duration of {CONFIG.job.max_vod_duration_hours} hours",
                JobErrorType.MEDIA_ERROR,
                recoverable=False
            )

        if duration < CONFIG.job.min_vod_duration_seconds:
            raise VODDownloadError(
                f"VOD is too short (minimum {CONFIG.job.min_vod_duration_seconds}s)",
                JobErrorType.MEDIA_ERROR,
                recoverable=False
            )

        print(f"VOD downloaded: {video_file}")
        print(f"Duration: {duration:.1f}s ({duration/3600:.2f}h)")
        print(f"Size: {video_file.stat().st_size / (1024*1024):.1f} MB")

        job_manager.update_state(job_id, JobState.DOWNLOAD_VALIDATED, 100, "Download validated")
        _report(100, "Download complete")

        return video_file, duration, title, streamer_name

    except VODDownloadError:
        raise
    except Exception as e:
        traceback.print_exc()
        error_msg = str(e).strip() or f"{type(e).__name__} (no message)"

        # yt-dlp KICK VOD extractor returning 404
        if "kick:vod" in error_msg or "Unable to download JSON metadata" in error_msg or "HTTP Error 404" in error_msg:
            raise VODDownloadError(
                "KICK VOD download failed (HTTP 404). This VOD may not exist, "
                "may be private, or KICK's API may have changed.\n\n"
                "Try using a CLIP instead:\n"
                "`!clip https://kick.com/username/clips/12345678`\n\n"
                "Clips are shorter and more reliably downloaded.",
                JobErrorType.DOWNLOAD_ERROR,
                recoverable=False
            )

        raise VODDownloadError(f"Download failed: {error_msg}", JobErrorType.DOWNLOAD_ERROR)


def _download_with_ytdlp(url: str, ydl_opts: dict) -> dict:
    """Synchronous yt-dlp download."""
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    return info


def _make_progress_hook(job_manager: JobManager, job_id: str, report_callback: Callable):
    """Create a progress hook for yt-dlp."""
    def hook(d):
        if d['status'] == 'downloading':
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            if total > 0:
                progress = (downloaded / total) * 90  # Reserve 10% for post-processing
                speed = d.get('speed', 0)
                eta = d.get('eta', 0)
                speed_str = f"{speed/1024/1024:.1f} MB/s" if speed else "N/A"
                eta_str = f"{eta}s" if eta else "N/A"
                report_callback(progress, f"Downloading... {speed_str}, ETA: {eta_str}")
        elif d['status'] == 'finished':
            report_callback(95, "Download finished, processing...")
    return hook


def _find_downloaded_video(job_dir: Path, info: dict) -> Optional[Path]:
    """Find the downloaded video file."""
    # First try the requested path from info
    if info.get('_filename'):
        requested = Path(info['_filename'])
        if requested.exists():
            return requested

    # Fallback: search for video files
    candidates = []
    for ext in ['.mp4', '.mkv', '.webm', '.mov']:
        candidates.extend(job_dir.glob(f"*{ext}"))

    if not candidates:
        return None

    # Prefer mp4, then largest
    candidates.sort(key=lambda p: (0 if p.suffix == '.mp4' else 1, -p.stat().st_size))
    return candidates[0]


def get_video_duration(video_path: Path) -> float:
    """Get video duration using ffprobe."""
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def is_kick_vod_url(url: str) -> bool:
    """Check if URL is a KICK VOD (not clip) URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url.strip())
        host = (parsed.hostname or "").lower()
        if host not in {"kick.com", "www.kick.com"}:
            return False
        path = (parsed.path or "").lower()
        # VOD URLs contain /videos/ or /video/
        return "/videos/" in path or "/video/" in path
    except Exception:
        return False


def extract_streamer_from_vod_url(url: str) -> Optional[str]:
    """Extract streamer username from KICK VOD URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url.strip())
        path = (parsed.path or "").strip("/")
        segments = [s for s in path.split("/") if s]
        if segments:
            return segments[0]
    except Exception:
        pass
    return None


async def probe_media(video_path: Path, job_id: str, job_manager: JobManager) -> dict:
    """
    Probe media file for detailed info.

    Returns dict with: width, height, fps, codec, bitrate, audio_codec, etc.
    """
    job_manager.update_state(job_id, JobState.MEDIA_PROBED, 0, "Probing media...")

    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "stream=width,height,r_frame_rate,codec_name,bitrate:format=duration,bitrate",
            "-of", "json",
            str(video_path)
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        import json
        data = json.loads(result.stdout)

        video_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), {})
        audio_stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'audio'), {})

        # Parse framerate
        fps_str = video_stream.get('r_frame_rate', '30/1')
        num, den = map(int, fps_str.split('/'))
        fps = num / den if den else 30

        info = {
            "width": video_stream.get('width', 1920),
            "height": video_stream.get('height', 1080),
            "fps": fps,
            "video_codec": video_stream.get('codec_name', 'h264'),
            "video_bitrate": int(video_stream.get('bitrate', 0) or 0),
            "audio_codec": audio_stream.get('codec_name', 'aac'),
            "audio_bitrate": int(audio_stream.get('bitrate', 0) or 0),
            "duration": float(data.get('format', {}).get('duration', 0)),
            "format_bitrate": int(data.get('format', {}).get('bitrate', 0) or 0),
        }

        job_manager.update_progress(job_id, 100, "Media probed")
        return info

    except Exception as e:
        traceback.print_exc()
        raise VODDownloadError(f"Media probe failed: {e}", JobErrorType.MEDIA_ERROR)


def cleanup_download(job_id: str):
    """Clean up download directory for a job."""
    job_dir = CONFIG.paths.downloads_dir / f"vod_{job_id}"
    if job_dir.exists():
        shutil.rmtree(job_dir, ignore_errors=True)