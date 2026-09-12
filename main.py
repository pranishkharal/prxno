import random
import os
import re
import difflib
import subprocess
import tempfile
import asyncio
import time
import threading
from captioning import transcribe_and_caption
from smart_cut import smart_cut
from word_caption import burn_word_captions
from clip_intelligence import build_moment_score
from clip_history import record_clip, is_duplicate, search_similar_transcript
from smart_presets import get_preset, list_presets
import shutil
import traceback
from pathlib import Path
from urllib.parse import urlparse
import sys

import webrtcvad
import discord
from public_download import create_public_download_link, start_cleanup_task, ensure_upload_tunnel, get_upload_public_url
from dotenv import load_dotenv
from rapidocr_onnxruntime import RapidOCR

# yt-dlp is used to download KICK Clips from their public Clip URL.
# Install it with:
#   python -m pip install -U yt-dlp curl_cffi
import yt_dlp

# VOD Intelligence Pipeline
from vod_intelligence import (
    get_job_manager,
    run_clip_pipeline,
    start_review_session,
    VODJob,
    JobState,
    ContentType,
    ClipCandidate,
    is_kick_vod_url,
    extract_streamer_from_vod_url,
    generate_metadata_for_all_clips,
)


load_dotenv()

print("MAIN: before upload tunnel thread")
def _start_upload_tunnel_background():
    try:
        print("UPLOAD TUNNEL: starting...")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        url = loop.run_until_complete(ensure_upload_tunnel())
        if url:
            print("UPLOAD TUNNEL: ready:", url)
        else:
            print("UPLOAD TUNNEL: could not start")
    except Exception as e:
        print("UPLOAD TUNNEL: error:", e)

threading.Thread(target=_start_upload_tunnel_background, daemon=True).start()
print("UPLOAD TUNNEL: thread started")

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN is not set. Put it in .env or set it in PowerShell."
    )

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("output")
OVERLAY_DIR = Path("overlays")

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
OVERLAY_DIR.mkdir(exist_ok=True)

# Maximum time allowed for FFmpeg jobs.
FFMPEG_TIMEOUT = 1800

# KICK Clips are currently documented by KICK as 10-180 seconds.
# We trim the final 4 seconds as requested, but never allow the result
# to become shorter than 1 second.


# ---------------------------------------------------------
# OCR ENGINE
# ---------------------------------------------------------

OCR_ENGINE = RapidOCR()

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

# Active editing sessions
SESSIONS = {}


# ---------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------

# Limits how many ffmpeg/edit jobs run at the same time.
# Extra jobs beyond this wait automatically instead of overloading the CPU.
MAX_CONCURRENT_EDITS = 10
MAX_CONCURRENT_DOWNLOADS = 5
EDIT_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_EDITS)
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

async def run_encode_job(func, *args):
    async with EDIT_SEMAPHORE:
        return await asyncio.to_thread(func, *args)


async def run_download_job(func, *args):
    async with DOWNLOAD_SEMAPHORE:
        return await asyncio.to_thread(func, *args)

def run_ffmpeg(command, timeout=FFMPEG_TIMEOUT):
    print("\n========================================")
    print("RUNNING FFMPEG")
    print("========================================")
    print(" ".join(str(x) for x in command))
    print("")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    output_lines = []

    try:
        for line in process.stdout:
            line = line.rstrip()

            if line:
                print(line)
                output_lines.append(line)

                if len(output_lines) > 1000:
                    output_lines.pop(0)

        return_code = process.wait(timeout=timeout)

    except subprocess.TimeoutExpired:
        process.kill()

        try:
            process.wait(timeout=10)
        except Exception:
            pass

        print("\nFFMPEG TIMEOUT")
        raise RuntimeError(
            f"FFmpeg timed out after {timeout} seconds."
        )

    except KeyboardInterrupt:
        process.kill()

        try:
            process.wait(timeout=10)
        except Exception:
            pass

        print("\nFFMPEG INTERRUPTED BY USER")
        raise

    if return_code != 0:
        print("\n========================================")
        print("FFMPEG ERROR")
        print("========================================")

        if output_lines:
            print("\n".join(output_lines[-200:]))

        raise RuntimeError(
            f"FFmpeg failed with exit code {return_code}"
        )

    print("\nFFmpeg finished successfully.")
    return return_code


def get_video_size(input_file):
    command = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        str(input_file)
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    value = result.stdout.strip()

    try:
        width, height = value.split("x")
        return int(width), int(height)
    except Exception:
        return 1920, 1080


def get_video_duration(input_file):
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_file)
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    try:
        return float(result.stdout.strip())
    except Exception:
        return 60.0


def upload_to_transfersh(file_path, timeout=120):
    """
    Upload a file to transfer.sh using the bundled curl-cffi.exe if available.
    Returns the public URL on success or None on failure.
    """
    file_path = Path(file_path)

    try:
        scripts_dir = Path(sys.executable).parent / "Scripts"
        curl_exe = scripts_dir / "curl-cffi.exe"

        if not curl_exe.exists():
            curl_cmd = "curl"
        else:
            curl_cmd = str(curl_exe)

        cmd = [
            curl_cmd,
            "-s",
            "--upload-file",
            str(file_path),
            f"https://transfer.sh/{file_path.name}"
        ]

        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )

        if result.returncode != 0:
            print("transfer.sh upload failed:", result.stderr.strip())
            return None

        url = result.stdout.strip()

        if url.startswith("http"):
            return url

    except Exception as e:
        print("transfer.sh upload error:", e)

    return None


def reencode_to_target_size(input_file, output_file, target_mb, presets=None, timeout=1800):
    """
    Try re-encoding the input_file to fit under target_mb by iterating
    over a small set of scale and CRF presets. Returns the output_path
    on success or None on failure.
    """
    input_file = Path(input_file)
    output_file = Path(output_file)

    if presets is None:
        presets = [
            # (scale_width, crf)
            (1280, 28),
            (1280, 30),
            (960, 28),
            (960, 30),
            (720, 28),
            (720, 30),
            (None, 32),
        ]

    for scale_w, crf in presets:

        if scale_w:
            vf = f"scale='min({scale_w},iw)':-2"
            out_name = f"compressed_{scale_w}_crf{crf}.mp4"
        else:
            vf = None
            out_name = f"compressed_crf{crf}.mp4"

        candidate = output_file.parent / (
            f"{output_file.stem.rsplit('.',1)[0]}_{out_name}"
        )

        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_file),
        ]

        if vf:
            cmd += ["-vf", vf]

        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            str(crf),
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            str(candidate)
        ]

        try:
            run_ffmpeg(cmd, timeout=timeout)

            if candidate.exists():
                size_mb = candidate.stat().st_size / (1024 * 1024)
                print(f"Compressed attempt: {size_mb:.2f} MB ({candidate})")
                if size_mb <= target_mb:
                    return candidate

                # keep candidate as potential smaller intermediate
                # but continue trying other presets

        except Exception as e:
            print("Re-encode attempt failed:", e)
            continue

    return None


# ---------------------------------------------------------
# KICK URL validation + downloader
# ---------------------------------------------------------

def is_kick_clip_url(url):
    """
    Accept normal KICK Clip URLs while rejecting arbitrary URLs.

    KICK URLs can change their exact path format, so validation is
    intentionally based on the hostname plus the presence of a
    clip-like path.
    """

    try:
        parsed = urlparse(url.strip())
    except Exception:
        return False

    host = (parsed.hostname or "").lower()

    if host not in {"kick.com", "www.kick.com"}:
        return False

    path = (parsed.path or "").lower()

    return (
        "/clip/" in path
        or "/clips/" in path
    )


def extract_streamer_from_url(url):
    """
    Extract the creator/streamer username directly from a KICK Clip URL.

    KICK Clip URLs follow the pattern:
        https://kick.com/<username>/clips/<clip_id>

    The first path segment is always the streamer's username, so this
    is a fast and reliable way to identify the streamer without
    needing OCR at all. OCR is kept as a fallback in case the
    username doesn't match any overlay filename we have on file.
    """

    try:
        parsed = urlparse(url.strip())
        path = (parsed.path or "").strip("/")
        segments = [segment for segment in path.split("/") if segment]

        if segments:
            return segments[0]

    except Exception:
        pass

    return None


def clean_url_from_message(text):
    """
    Extract the first HTTP(S) URL from a Discord message.
    """

    match = re.search(
        r"https?://[^\s<>]+",
        text.strip()
    )

    if not match:
        return None

    url = match.group(0).rstrip(".,)>]}")

    return url


def download_kick_clip(url, user_id):
    """
    Download a KICK Clip to the local USB uploads directory.

    yt-dlp currently has a KICK Clips extractor. The download is
    temporary and is deleted after editing finishes.

    curl_cffi is recommended because some KICK endpoints can reject
    ordinary HTTP clients.
    """

    if not is_kick_clip_url(url):
        raise RuntimeError(
            "That does not look like a KICK Clip URL.\n"
            "Please paste a link such as https://kick.com/.../clip/..."
        )

    # Unique temporary working directory prevents two users from
    # accidentally overwriting each other's source file.
    job_dir = UPLOAD_DIR / f"kick_{user_id}"

    if job_dir.exists():
        for old_file in job_dir.glob("*"):
            _safe_remove(old_file)
        _safe_remove(job_dir)

    job_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(
        job_dir / "kick_clip_%(id)s.%(ext)s"
    )

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
        "continuedl": True,
        "nopart": True,
    }

    # Prefer curl_cffi impersonation when the installed yt-dlp version
    # supports it. Newer yt-dlp versions require an ImpersonateTarget
    # object here (not a plain string), or they raise an AssertionError.
    # If anything about impersonation isn't available, fall back to
    # yt-dlp's normal client instead of crashing.
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget
        ydl_opts["impersonate"] = ImpersonateTarget.from_str("chrome")
    except Exception as impersonate_error:
        print(
            f"Impersonation unavailable, continuing without it: "
            f"{impersonate_error}"
        )

    print("")
    print("========================================")
    print(" DOWNLOADING KICK CLIP")
    print("========================================")
    print(url)

    max_download_attempts = 3
    last_error = None

    for attempt in range(max_download_attempts):
        try:
            print(f"CHECKPOINT: download attempt {attempt + 1}/{max_download_attempts}", flush=True)

            # Small delay before download to let any file locks release
            if attempt > 0:
                time.sleep(2.0)

            # Clean job directory before each attempt
            if job_dir.exists():
                for old_file in job_dir.glob("*"):
                    _safe_remove(old_file)
                _safe_remove(job_dir)
            job_dir.mkdir(parents=True, exist_ok=True)

            print("CHECKPOINT: about to call yt_dlp.YoutubeDL(...)", flush=True)
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                print("CHECKPOINT: calling ydl.extract_info(...) now", flush=True)
                info = ydl.extract_info(url, download=True)
                print("CHECKPOINT: extract_info() returned", flush=True)

            requested_path = None

            if info:
                requested_path = info.get("_filename")

            candidates = list(job_dir.glob("*"))

            video_candidates = [
                p for p in candidates
                if p.is_file()
                and p.suffix.lower() in {
                    ".mp4", ".mov", ".mkv", ".webm"
                }
            ]

            if requested_path:
                requested = Path(requested_path)
                if requested.exists():
                    video_candidates.insert(0, requested)

            if not video_candidates:
                raise RuntimeError(
                    "KICK download completed, but no video file was found."
                )

            # Prefer MP4.
            video_candidates.sort(
                key=lambda p: (
                    0 if p.suffix.lower() == ".mp4" else 1,
                    -p.stat().st_size
                )
            )

            video_file = video_candidates[0]

            if video_file.stat().st_size < 10_000:
                raise RuntimeError(
                    "The downloaded KICK file is unexpectedly small."
                )

            print(f"KICK clip downloaded: {video_file}")

            return video_file

        except Exception as e:
            last_error = e
            print(f"Download attempt {attempt + 1} failed: {e}")
            traceback.print_exc()

            # Clean up any partial files
            if job_dir.exists():
                for temp_file in job_dir.glob("*"):
                    _safe_remove(temp_file)

            # If this is the last attempt, break and handle the error
            if attempt < max_download_attempts - 1:
                print(f"Retrying download in 2 seconds...")
                continue
            else:
                break

    # All attempts failed
    if last_error is not None:
        message = str(last_error).strip() or f"{type(last_error).__name__} (no message)"

        if "WinError 5" in message or "Access is denied" in message or "WinError 32" in message:
            user_msg = (
                "Windows file lock error during download.\n\n"
                "This happens when another process still has the file open.\n\n"
                "Solutions:\n"
                "1. Close any file explorer windows showing the uploads folder\n"
                "2. Disable antivirus real-time scanning for the project folder\n"
                "3. Restart the bot to release file locks\n"
                "4. Run: PowerShell as Admin → "
                "'icacls uploads /grant Everyone:(OI)(CI)F' to fix permissions"
            )
        else:
            user_msg = (
                "Could not download the KICK Clip.\n"
                f"{message}\n\n"
                "Make sure the Clip opens normally in your browser and "
                "that yt-dlp/curl_cffi are installed."
            )

        raise RuntimeError(user_msg)


def _safe_remove(path: Path, retries: int = 5, delay: float = 0.5):
    if not path.exists():
        return True
    for attempt in range(retries):
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                return True
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=False)
                return True
        except PermissionError:
            if attempt < retries - 1:
                time.sleep(delay)
            else:
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            return False
    return False


def cleanup_job_files(path):
    """
    Delete a downloaded KICK source and its temporary job directory.
    Uses retry logic to handle Windows file locks.
    """

    try:
        path = Path(path)

        if path.exists():
            _safe_remove(path)

        parent = path.parent

        if (
            parent.parent.resolve() == UPLOAD_DIR.resolve()
            and parent.name.startswith("kick_")
        ):
            _safe_remove(parent)

    except Exception as e:
        print("Cleanup warning:", e)


# ---------------------------------------------------------
# Trim a finished video down to fit under a size limit
# ---------------------------------------------------------

def trim_to_target_size(input_file, output_file, target_mb, safety_margin=0.95):
    """
    Trim the END off an already-encoded video so the file fits under
    target_mb, WITHOUT re-encoding or lowering bitrate/quality at all.

    This uses "-c copy" (stream copy), so the video/audio data itself
    is untouched -- only its length is shortened. A small safety
    margin is applied since stream-copy cuts snap to the nearest
    keyframe rather than an exact byte count.
    """

    input_file = Path(input_file)
    output_file = Path(output_file)

    duration = get_video_duration(input_file)
    size_bytes = input_file.stat().st_size

    if duration <= 0 or size_bytes <= 0:
        shutil.copy2(input_file, output_file)
        return

    bytes_per_second = size_bytes / duration
    target_bytes = target_mb * 1024 * 1024 * safety_margin
    target_duration = target_bytes / bytes_per_second

    # Never go below 1 second, and never "extend" past the original.
    target_duration = max(1.0, min(target_duration, duration))

    if target_duration >= duration - 0.05:
        shutil.copy2(input_file, output_file)
        return

    print("")
    print("========================================")
    print(" TRIMMING TO FIT SIZE LIMIT (same bitrate)")
    print("========================================")
    print(
        f"Original: {duration:.2f}s / "
        f"{size_bytes / (1024 * 1024):.2f} MB"
    )
    print(
        f"Target: {target_duration:.2f}s "
        f"(aiming for under {target_mb:.1f} MB)"
    )

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_file),
        "-t",
        f"{target_duration:.3f}",
        "-c",
        "copy",
        str(output_file)
    ]

    run_ffmpeg(command)


# ---------------------------------------------------------
# Remove final 4 seconds
# ---------------------------------------------------------

def remove_silence(input_file, output_file):
    print("")
    print("========================================")
    print(" VOICE ACTIVITY DETECTION")
    print("========================================")
    print("Detecting actual speech...")

    input_file = Path(input_file)
    output_file = Path(output_file)

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_file),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "-"
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Audio extraction timed out while detecting speech."
        )

    if result.returncode != 0:
        raise RuntimeError(
            "Could not extract audio for speech detection."
        )

    pcm = result.stdout

    if not pcm:
        print("No audio detected.")
        print("Keeping original video.")

        run_ffmpeg([
            "ffmpeg",
            "-y",
            "-i",
            str(input_file),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_file)
        ])

        return

    vad = webrtcvad.Vad(2)

    sample_rate = 16000
    frame_ms = 30
    samples_per_frame = int(
        sample_rate * frame_ms / 1000
    )
    bytes_per_frame = samples_per_frame * 2

    speech_frames = []

    total_frames = len(pcm) // bytes_per_frame

    for index in range(total_frames):

        start_byte = index * bytes_per_frame
        end_byte = start_byte + bytes_per_frame

        frame = pcm[start_byte:end_byte]

        try:
            is_speech = vad.is_speech(
                frame,
                sample_rate
            )
        except Exception:
            is_speech = False

        if is_speech:
            speech_frames.append(index)

    if not speech_frames:
        print("No speech detected.")
        print("Keeping original video.")

        run_ffmpeg([
            "ffmpeg",
            "-y",
            "-i",
            str(input_file),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_file)
        ])

        return

    raw_intervals = []

    interval_start = speech_frames[0]
    previous = speech_frames[0]

    for frame_index in speech_frames[1:]:

        if frame_index - previous > 10:
            start_time = (
                interval_start
                * frame_ms
                / 1000
            )

            end_time = (
                (previous + 1)
                * frame_ms
                / 1000
            )

            raw_intervals.append(
                (start_time, end_time)
            )

            interval_start = frame_index

        previous = frame_index

    raw_intervals.append(
        (
            interval_start * frame_ms / 1000,
            (previous + 1) * frame_ms / 1000
        )
    )

    merged = []

    for start_time, end_time in raw_intervals:

        if not merged:
            merged.append(
                [start_time, end_time]
            )
            continue

        previous_start, previous_end = merged[-1]

        gap = start_time - previous_end

        if gap <= 0.75:
            merged[-1][1] = end_time
        else:
            merged.append(
                [start_time, end_time]
            )

    padding_before = 0.25
    padding_after = 0.35

    duration = get_video_duration(input_file)

    intervals = []

    for start_time, end_time in merged:

        start_time = max(
            0.0,
            start_time - padding_before
        )

        end_time = min(
            duration,
            end_time + padding_after
        )

        if end_time - start_time >= 0.20:
            intervals.append(
                (start_time, end_time)
            )

    final_intervals = []

    for start_time, end_time in intervals:

        if not final_intervals:
            final_intervals.append(
                [start_time, end_time]
            )
            continue

        previous_start, previous_end = final_intervals[-1]

        if start_time <= previous_end:
            final_intervals[-1][1] = max(
                previous_end,
                end_time
            )
        else:
            final_intervals.append(
                [start_time, end_time]
            )

    print(
        f"Detected {len(final_intervals)} speech sections."
    )

    for index, (start_time, end_time) in enumerate(
        final_intervals,
        start=1
    ):
        print(
            f"Speech {index}: "
            f"{start_time:.2f}s -> {end_time:.2f}s"
        )

    original_duration = duration

    kept_duration = sum(
        end_time - start_time
        for start_time, end_time in final_intervals
    )

    removed_duration = max(
        0.0,
        original_duration - kept_duration
    )

    print(
        f"Original duration: {original_duration:.2f}s"
    )
    print(
        f"Speech kept: {kept_duration:.2f}s"
    )
    print(
        f"Non-speaking removed: "
        f"{removed_duration:.2f}s"
    )

    if (
        len(final_intervals) == 1
        and final_intervals[0][0] <= 0.01
        and final_intervals[0][1] >= original_duration - 0.05
    ):

        print("No meaningful pauses found.")
        print("Keeping complete video.")

        run_ffmpeg([
            "ffmpeg",
            "-y",
            "-i",
            str(input_file),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(output_file)
        ])

        return

    filters = []

    for index, (start_time, end_time) in enumerate(
        final_intervals
    ):

        filters.append(
            f"[0:v]"
            f"trim=start={start_time}:end={end_time},"
            f"setpts=PTS-STARTPTS"
            f"[v{index}]"
        )

        filters.append(
            f"[0:a]"
            f"atrim=start={start_time}:end={end_time},"
            f"asetpts=PTS-STARTPTS"
            f"[a{index}]"
        )

    concat_inputs = ""

    for index in range(len(final_intervals)):
        concat_inputs += (
            f"[v{index}][a{index}]"
        )

    filters.append(
        concat_inputs
        + f"concat=n={len(final_intervals)}:v=1:a=1[v][a]"
    )

    filter_complex = ";".join(filters)

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_file),
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_file)
    ]

    run_ffmpeg(command)

    print("")
    print("========================================")
    print(" SPEECH TRIM COMPLETE")
    print("========================================")


# ---------------------------------------------------------
# AUTOMATIC OCR OVERLAY SYSTEM
# ---------------------------------------------------------

OVERLAY_EXTENSIONS = {
    ".webp",
    ".png",
    ".jpg",
    ".jpeg",
}


def normalize_streamer_text(value):
    return re.sub(
        r"[^a-z0-9]+",
        "",
        str(value).lower()
    )


def overlay_base_name(path):
    stem = path.stem.lower().strip()

    stem = re.sub(
        r"\s*\(\d+\)\s*$",
        "",
        stem
    )

    stem = re.sub(
        r"_(916|11|45|43)$",
        "",
        stem
    )

    return normalize_streamer_text(stem)


def get_overlay_library():
    library = {}

    for path in OVERLAY_DIR.iterdir():

        if not path.is_file():
            continue

        if path.suffix.lower() not in OVERLAY_EXTENSIONS:
            continue

        base = overlay_base_name(path)

        if not base or base == "kick":
            continue

        library.setdefault(
            base,
            []
        ).append(path)

    return library


def _ocr_texts(image_path):
    result = OCR_ENGINE(
        str(image_path)
    )

    texts = getattr(
        result,
        "txts",
        None
    )

    if texts:
        return [
            str(text)
            for text in texts
            if str(text).strip()
        ]

    if isinstance(result, tuple) and result:

        rows = result[0]

        if rows:

            output = []

            for row in rows:

                if len(row) > 1:

                    text = str(
                        row[1]
                    )

                    if text.strip():
                        output.append(text)

            return output

    return []


def detect_streamer_name(input_file):
    library = get_overlay_library()

    if not library:
        print(
            "OCR: No named overlays found in overlays folder."
        )
        return None

    print("")
    print("========================================")
    print(" OCR STREAMER DETECTION")
    print("========================================")

    print(
        "Available streamer overlays:",
        ", ".join(
            sorted(library.keys())
        )
    )

    duration = max(
        get_video_duration(input_file),
        1.0
    )

    sample_ratios = [
        0.05,
        0.20,
        0.40,
        0.60,
        0.80,
        0.95,
    ]

    matches = {
        name: []
        for name in library
    }

    with tempfile.TemporaryDirectory(
        prefix="cfa_ocr_"
    ) as temp_dir:

        temp_dir = Path(
            temp_dir
        )

        for frame_index, ratio in enumerate(
            sample_ratios
        ):

            second = min(
                max(
                    duration * ratio,
                    0.1
                ),
                max(
                    duration - 0.1,
                    0.1
                )
            )

            variants = [
                (
                    "full",
                    "scale=1600:-2"
                ),
                (
                    "name_area",
                    "crop=iw*0.9:ih*0.65:iw*0.05:0,"
                    "scale=1600:-2"
                ),
            ]

            for variant_index, (
                variant_name,
                video_filter
            ) in enumerate(variants):

                frame = (
                    temp_dir
                    / f"frame_{frame_index}_{variant_index}.jpg"
                )

                command = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    str(second),
                    "-i",
                    str(input_file),
                    "-an",
                    "-threads",
                    "1",
                    "-frames:v",
                    "1",
                    "-vf",
                    video_filter,
                    "-q:v",
                    "2",
                    "-y",
                    str(frame)
                ]

                try:
                    result = subprocess.run(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=30
                    )
                except subprocess.TimeoutExpired:
                    print(
                        f"OCR frame {frame_index + 1} "
                        f"({variant_name}) timed out."
                    )
                    continue

                if (
                    result.returncode != 0
                    or not frame.exists()
                ):
                    continue

                try:
                    texts = _ocr_texts(frame)
                except Exception as ocr_error:
                    print(
                        "OCR frame error:",
                        ocr_error
                    )
                    continue

                if texts:
                    print(
                        f"OCR frame {frame_index + 1} "
                        f"({variant_name}): "
                        f"{texts}"
                    )

                for text in texts:

                    normalized_text = (
                        normalize_streamer_text(
                            text
                        )
                    )

                    if len(normalized_text) < 3:
                        continue

                    for overlay_name in library:

                        candidate = (
                            normalize_streamer_text(
                                overlay_name
                            )
                        )

                        if len(candidate) < 3:
                            continue

                        if candidate in normalized_text:
                            score = 1.0

                        elif (
                            normalized_text in candidate
                            and len(normalized_text) >= 4
                        ):
                            score = 0.90

                        else:
                            score = (
                                difflib.SequenceMatcher(
                                    None,
                                    candidate,
                                    normalized_text
                                ).ratio()
                            )

                        if score >= 0.72:
                            matches[
                                overlay_name
                            ].append(score)

    ranked = []

    for overlay_name, scores in matches.items():

        if not scores:
            continue

        scores = sorted(
            scores,
            reverse=True
        )

        exact_matches = sum(
            1
            for score in scores
            if score >= 0.99
        )

        strong_matches = sum(
            1
            for score in scores
            if score >= 0.82
        )

        combined_score = (
            scores[0]
            + min(
                sum(scores[1:3]),
                0.50
            )
        )

        if (
            exact_matches >= 1
            or strong_matches >= 2
            or scores[0] >= 0.90
        ):
            ranked.append(
                (
                    combined_score,
                    scores[0],
                    overlay_name
                )
            )

    if not ranked:

        print(
            "OCR: Could not confidently identify streamer."
        )

        print(
            "OCR: No streamer overlay will be applied."
        )

        return None

    ranked.sort(
        reverse=True
    )

    best_score, top_score, streamer = ranked[0]

    print(
        f"OCR: Detected streamer = {streamer}"
    )

    print(
        f"OCR: confidence score = {best_score:.2f}"
    )

    print("========================================")
    print("")

    return streamer


def find_overlay_for_streamer(
    streamer_name,
    size
):
    library = get_overlay_library()

    normalized_name = (
        normalize_streamer_text(
            streamer_name
        )
    )

    files = library.get(
        normalized_name,
        []
    )

    if not files:

        print(
            f"No overlay found for streamer: "
            f"{streamer_name}"
        )

        return None

    suffix_map = {
        "9:16": "_916",
        "1:1": "_11",
        "4:5": "_45",
        "4:3": "_43",
        "Original": "",
    }

    preferred_suffix = (
        suffix_map.get(
            size,
            ""
        )
    )

    if preferred_suffix:

        preferred = [
            path
            for path in files
            if path.stem.lower().endswith(
                preferred_suffix
            )
        ]

    else:

        preferred = [
            path
            for path in files
            if not re.search(
                r"_(916|11|45|43)$",
                path.stem.lower()
            )
        ]

    pool = (
        preferred
        if preferred
        else files
    )

    webp = [
        path
        for path in pool
        if path.suffix.lower() == ".webp"
    ]

    selected = (
        webp[0]
        if webp
        else pool[0]
    )

    print(
        f"Automatic overlay selected: {selected}"
    )

    return selected


def overlay_position(size):
    if size == "9:16":
        return "H-h-(H*0.25)"

    if size == "1:1":
        return "H-h-(H*0.06)"

    if size == "4:5":
        return "H-h-(H*0.10)"

    if size == "4:3":
        return "H-h-(H*0.08)"

    return "H-h-(H*0.08)"


def get_overlay_file(
    size,
    input_file=None,
    url_streamer_hint=None
):
    # -------------------------------------------------------
    # 1) Try the streamer name straight from the KICK URL first.
    #    This is instant and far more reliable than OCR, since it
    #    comes directly from KICK rather than being read off-screen.
    # -------------------------------------------------------

    if url_streamer_hint:

        print(
            f"Checking for overlay matching URL streamer name: "
            f"{url_streamer_hint}"
        )

        overlay = find_overlay_for_streamer(
            url_streamer_hint,
            size
        )

        if overlay:

            print(
                f"Overlay matched from URL: {url_streamer_hint}"
            )

            return overlay

        print(
            f"No overlay found for URL streamer name "
            f"'{url_streamer_hint}'. Falling back to OCR."
        )

    # -------------------------------------------------------
    # 2) Fall back to OCR-based detection from the video itself.
    # -------------------------------------------------------

    if input_file is None:
        print(
            "Overlay detection requires the input video."
        )
        return None

    streamer = detect_streamer_name(
        input_file
    )

    if not streamer:
        return None

    return find_overlay_for_streamer(
        streamer,
        size
    )


# ---------------------------------------------------------
# Picture enhancement presets (sharpness / contrast / saturation)
# ---------------------------------------------------------
# "eq" adjusts contrast and saturation, "unsharp" adjusts sharpness.
# These are combined into a single FFmpeg filter string per level.

ENHANCE_PRESETS = {
    "Off": None,
    "Subtle": (
        "eq=contrast=1.20:saturation=1.35:brightness=0.02:gamma=1.03,"
        "unsharp=5:5:1.2:5:5:0.5"
    ),
    "Vivid": (
        "eq=contrast=1.35:saturation=1.60:brightness=0.03:gamma=1.05,"
        "unsharp=5:5:2.0:5:5:0.8"
    ),
}


# ---------------------------------------------------------
# Video editing
# ---------------------------------------------------------

def edit_video(input_file, output_file, options, overlay_file=None, url_streamer_hint=None, split_photo_file=None):

    size = options["size"]

    if options.get("overlay", True) and overlay_file is None:
        overlay_file = get_overlay_file(
            size,
            input_file,
            url_streamer_hint
        )

    zoom = options["zoom"]
    mirror = options["mirror"]
    blur = options["blur"]

    print(
        f"Editing {input_file} with "
        f"{options}"
    )

    dimensions = {
        "9:16": (1080, 1920),
        "1:1": (1080, 1080),
        "4:5": (1080, 1350),
        "4:3": (1440, 1080),
        "Original": None
    }

    target = dimensions.get(size)

    filters = []

    if target is None:

        width, height = get_video_size(input_file)

        out_w = width
        out_h = height

        if mirror:
            filters.append("hflip")

        if zoom:
            filters.append(
                "scale=iw*1.08:ih*1.08,"
                "crop=floor(iw/1.08/2)*2:floor(ih/1.08/2)*2"
            )

        enhance_filter = ENHANCE_PRESETS.get(
            options.get("enhance", "Off")
        )

        if enhance_filter:
            filters.append(enhance_filter)

        video_filter = ",".join(filters) if filters else "null"

        if overlay_file:
            command = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_file),
                "-loop",
                "1",
                "-i",
                str(overlay_file),
                "-filter_complex",
                (
                    f"[0:v]{video_filter}[v];"
                    f"[1:v]format=rgba,"
                    f"scale={out_w}:-1[ov];"
                    f"[v][ov]overlay="
                    f"(W-w)/2:H-h-(H*0.25):format=auto[out]"
                ),
                "-map",
                "[out]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(output_file)
            ]

        else:
            command = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_file),
                "-vf",
                video_filter,
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(output_file)
            ]

        run_ffmpeg(command)

        if size != "9:16":
            temp = output_file.parent / f".{output_file.name}.temp_916.mp4"
            print(f"Padding final output to 9:16 canvas for size={size}")
            run_ffmpeg([
                "ffmpeg",
                "-y",
                "-i",
                str(output_file),
                "-vf",
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(1080-iw)/2:(1920-ih)/2:color=black,"
                "setsar=1",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(temp)
            ])
            temp.replace(output_file)
        return

    # ---------------------------------------------------------
    # SPLIT SCREEN - USER UPLOADED IMAGE OR VIDEO
    # ---------------------------------------------------------
    if options.get("split_screen") and split_photo_file and target is not None:

        split_file = Path(split_photo_file)

        if split_file.exists():

            out_w, out_h = target
            half_h = out_h // 2

            image_extensions = {
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".gif"
            }

            is_image = split_file.suffix.lower() in image_extensions

            # TOP: original KICK clip
            filter_complex = (
                f"[0:v]"
                f"scale={out_w}:{half_h}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={out_w}:{half_h},"
                f"setsar=1"
                f"[top]"
            )

            top_name = "top"

            if mirror:
                filter_complex += (
                    f";[{top_name}]"
                    "hflip"
                    "[topmirror]"
                )
                top_name = "topmirror"

            if zoom:
                filter_complex += (
                    f";[{top_name}]"
                    f"scale={int(out_w * 1.08)}:{int(half_h * 1.08)},"
                    f"crop={out_w}:{half_h}"
                    "[topzoom]"
                )
                top_name = "topzoom"

            enhance_filter = ENHANCE_PRESETS.get(
                options.get("enhance", "Off")
            )

            if enhance_filter:
                filter_complex += (
                    f";[{top_name}]"
                    f"{enhance_filter}"
                    "[topenhanced]"
                )
                top_name = "topenhanced"

            filter_complex += (
                f";[{top_name}]"
                "setpts=PTS-STARTPTS"
                "[topfinal]"
            )

            # BOTTOM: uploaded image/video
            filter_complex += (
                ";[1:v]"
                f"scale={out_w}:{half_h}:"
                "force_original_aspect_ratio=increase,"
                f"crop={out_w}:{half_h},"
                "setsar=1,"
                "setpts=PTS-STARTPTS"
                "[bottom]"
            )

            # Combine top and bottom.
            filter_complex += (
                ";[topfinal][bottom]"
                "vstack=inputs=2"
                "[stacked]"
            )

            if overlay_file:

                # Existing KICK branding remains over the split layout.
                filter_complex += (
                    ";[2:v]"
                    "format=rgba,"
                    f"scale={out_w}:-1:"
                    "force_original_aspect_ratio=decrease"
                    "[branding];"
                    "[stacked][branding]"
                    "overlay="
                    "(W-w)/2:"
                    "(H-h)/2:"
                    "format=auto"
                    "[final]"
                )

                map_video = "[final]"

            else:
                map_video = "[stacked]"

            if is_image:

                background_input = [
                    "-loop",
                    "1",
                    "-i",
                    str(split_file)
                ]

            else:

                background_input = [
                    "-stream_loop",
                    "-1",
                    "-i",
                    str(split_file)
                ]

            command = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_file),
                *background_input
            ]

            if overlay_file:
                command += [
                    "-loop",
                    "1",
                    "-i",
                    str(overlay_file)
                ]

            command += [
                "-filter_complex",
                filter_complex,
                "-map",
                map_video,
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                str(output_file)
            ]

            print(
                f"Split Screen ACTIVE: {split_file.name} "
                f"({'image' if is_image else 'video'})"
            )

            run_ffmpeg(command)

            if size != "9:16":
                temp = output_file.parent / f".{output_file.name}.temp_916.mp4"
                print(f"Padding split-screen output to 9:16 canvas for size={size}")
                run_ffmpeg([
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(output_file),
                    "-vf",
                    "scale=1080:1920:force_original_aspect_ratio=decrease,"
                    "pad=1080:1920:(1080-iw)/2:(1920-ih)/2:color=black,"
                    "setsar=1",
                    "-c:v",
                    "libx264",
                    "-preset",
                    "medium",
                    "-crf",
                    "20",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    str(temp)
                ])
                temp.replace(output_file)
            return

        else:
            print(
                f"Split Screen file does not exist: {split_file}"
            )

    out_w, out_h = target

    # When blur mode is on, there's a real boundary line between the
    # sharp foreground video and the blurred padding around it. We
    # calculate exactly where that line sits (in pixels) so the
    # overlay can be placed precisely at that transition, instead of
    # a fixed percentage of the whole canvas.
    fg_line_y = None

    if blur:

        src_w, src_h = get_video_size(input_file)

        fit_scale = min(out_w / src_w, out_h / src_h)
        fg_scaled_h = src_h * fit_scale

        # Distance from the top of the canvas to the bottom edge of
        # the sharp (non-blurred) foreground content.
        fg_line_y = (out_h + fg_scaled_h) / 2

        if zoom:
            # The zoom effect scales the whole composed frame up by
            # 8% and crops back to the original canvas size from the
            # center. Apply that same transform to the boundary line
            # so it still lines up with the zoomed footage.
            zoom_scale = 1.08
            fg_line_y = (
                (fg_line_y * zoom_scale)
                - (out_h * (zoom_scale - 1) / 2)
            )

        print(
            f"Blur boundary line at y={fg_line_y:.1f}px "
            f"(canvas height={out_h}px, "
            f"foreground height={fg_scaled_h:.1f}px)"
        )

    if blur:

        foreground_scale = (
            f"scale={out_w}:{out_h}:"
            f"force_original_aspect_ratio=decrease"
        )

        filter_complex = (
            f"[0:v]split=2[bg][fg];"

            f"[bg]"
            f"scale={out_w}:{out_h}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},"
            f"boxblur=25:12,"
            f"setsar=1"
            f"[bgblur];"

            f"[fg]"
            f"{foreground_scale},"
            f"setsar=1"
            f"[fgscaled];"

            f"[bgblur][fgscaled]"
            f"overlay=(W-w)/2:(H-h)/2"
            f"[base]"
        )

    else:

        filter_complex = (
            f"[0:v]"
            f"scale={out_w}:{out_h}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},"
            f"setsar=1"
            f"[base]"
        )

    if zoom:

        filter_complex += (
            ";[base]"
            "scale=iw*1.08:ih*1.08,"
            "crop=floor(iw/1.08/2)*2:floor(ih/1.08/2)*2"
            "[zoomed]"
        )

        base_name = "zoomed"

    else:
        base_name = "base"

    if mirror:

        filter_complex += (
            f";[{base_name}]hflip[mirrored]"
        )

        base_name = "mirrored"

    enhance_filter = ENHANCE_PRESETS.get(
        options.get("enhance", "Off")
    )

    if enhance_filter:

        filter_complex += (
            f";[{base_name}]{enhance_filter}[enhanced]"
        )

        base_name = "enhanced"

    if overlay_file:

        # Place the overlay exactly at the sharp/blur boundary line
        # when blur mode is on. Otherwise fall back to the fixed
        # percentage-based position, since there's no natural boundary
        # to anchor to in flat crop-fill mode.
        overlay_y_expr = (
            f"{fg_line_y:.1f}"
            if fg_line_y is not None
            else overlay_position(size)
        )

        filter_complex += (
            f";[1:v]"
            f"format=rgba,"
            f"scale={out_w}:-1:"
            f"force_original_aspect_ratio=decrease"
            f"[overlay];"

            f"[{base_name}][overlay]"
            f"overlay="
            f"(W-w)/2:"
            f"{overlay_y_expr}:"
            f"format=auto"
            f"[final]"
        )

        map_video = "[final]"

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_file),
            "-loop",
            "1",
            "-i",
            str(overlay_file),
            "-filter_complex",
            filter_complex,
            "-map",
            map_video,
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(output_file)
        ]

    else:

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_file),
            "-filter_complex",
            filter_complex,
            "-map",
            f"[{base_name}]",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            str(output_file)
        ]

    run_ffmpeg(command)

    if size != "9:16":
        temp = output_file.parent / f".{output_file.name}.temp_916.mp4"
        print(f"Padding final output to 9:16 canvas for size={size}")
        run_ffmpeg([
            "ffmpeg",
            "-y",
            "-i",
            str(output_file),
            "-vf",
            "scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(1080-iw)/2:(1920-ih)/2:color=black,"
            "setsar=1",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(temp)
        ])
        temp.replace(output_file)


# ---------------------------------------------------------
# SPLIT SCREEN BACKGROUND VIDEO
# ---------------------------------------------------------

BACKGROUND_DIR = Path("backgrounds")

def get_split_background():
    """Return a random background video from backgrounds/."""
    BACKGROUND_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for ext in ("*.mp4", "*.mov", "*.webm", "*.mkv"):
        files.extend(BACKGROUND_DIR.glob(ext))
    if not files:
        return None
    return random.choice(files)

# UI
# ---------------------------------------------------------

class EditView(discord.ui.View):

    def __init__(self, user, file_path, url_streamer_hint=None, channel=None):

        super().__init__(timeout=900)

        self.user = user
        self.file_path = Path(file_path)
        self.channel = channel

        self.options = {
            "size": "9:16",
            "zoom": False,
            "mirror": False,
            "blur": False,
            "remove_silence": True,
            "overlay": True,
            "enhance": "Off",
            "split_screen": False,
            "auto_captions": False
        }

        self.overlay_file = None
        self.split_photo_file = None
        self.url_streamer_hint = url_streamer_hint
        self.started = False

    async def interaction_check(self, interaction):

        if interaction.user.id != self.user.id:

            await interaction.response.send_message(
                "Ã¢ÂÅ’ This edit panel belongs to someone else.",
                ephemeral=True
            )

            return False

        return True

    def apply_smart_preset(self, preset_name="TikTok"):
        preset = get_preset(preset_name)

        if not preset:
            return False

        # Apply the Smart Edit preset, but NEVER change Auto Captions.
        # Captions require explicit user permission.
        auto_captions_choice = self.options.get("auto_captions", False)

        for key, value in preset.items():
            if key == "auto_captions":
                continue
            self.options[key] = value

        self.options["auto_captions"] = auto_captions_choice
        self.options["smart_preset"] = preset_name

        return True


    def summary(self):

        enabled = []

        enabled.append(self.options["size"])

        if self.options["zoom"]:
            enabled.append("Zoom")

        if self.options["mirror"]:
            enabled.append("Mirror")

        if self.options["blur"]:
            enabled.append("Background Blur")

        if self.options["remove_silence"]:
            enabled.append("Remove Non-Speech")

        if self.options["overlay"]:
            enabled.append("Overlay")

        if self.options.get("enhance", "Off") != "Off":
            enabled.append(f"{self.options['enhance']} Enhance")

        if self.options.get("split_screen", False):
            enabled.append("Split Screen")

        if self.options.get("auto_captions", False):
            enabled.append("Auto Captions")

        return " â€¢ ".join(enabled)

    @discord.ui.select(
        placeholder="Ã°Å¸â€œÂ Choose video size",
        options=[
            discord.SelectOption(label="9:16 Vertical", value="9:16"),
            discord.SelectOption(label="1:1 Square", value="1:1"),
            discord.SelectOption(label="4:5 Portrait", value="4:5"),
            discord.SelectOption(label="4:3", value="4:3"),
            discord.SelectOption(label="Original", value="Original"),
        ]
    )
    async def size_select(self, interaction, select):

        self.options["size"] = select.values[0]

        await interaction.response.edit_message(
            content=(
                "**AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    @discord.ui.select(
        placeholder="Ã°Å¸Å½Â¨ Sharpness / Contrast / Saturation",
        options=[
            discord.SelectOption(
                label="Off",
                value="Off",
                description="No picture adjustment",
                default=True
            ),
            discord.SelectOption(
                label="Subtle Enhance",
                value="Subtle",
                description="Slight sharpness/contrast/saturation boost"
            ),
            discord.SelectOption(
                label="Vivid Enhance",
                value="Vivid",
                description="Stronger, more punchy look"
            ),
        ]
    )
    async def enhance_select(self, interaction, select):

        self.options["enhance"] = select.values[0]

        await interaction.response.edit_message(
            content=(
                "**AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    @discord.ui.button(
        label="Smart Edit",
        emoji="🧠",
        style=discord.ButtonStyle.success,
        row=4
    )
    async def smart_edit_button(self, interaction, button):
        if self.apply_smart_preset("TikTok"):
            await interaction.response.send_message(
                "🧠 Smart Edit enabled. "
                "TikTok quality preset applied.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "Unable to apply Smart Edit.",
                ephemeral=True
            )


    @discord.ui.button(
        label="Slightly Zoomed",
        style=discord.ButtonStyle.secondary
    )
    async def zoom_button(self, interaction, button):

        self.options["zoom"] = not self.options["zoom"]

        button.style = (
            discord.ButtonStyle.success
            if self.options["zoom"]
            else discord.ButtonStyle.secondary
        )

        await interaction.response.edit_message(
            content=(
                "**AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    @discord.ui.button(
        label="Mirror",
        style=discord.ButtonStyle.secondary
    )
    async def mirror_button(self, interaction, button):

        self.options["mirror"] = not self.options["mirror"]

        button.style = (
            discord.ButtonStyle.success
            if self.options["mirror"]
            else discord.ButtonStyle.secondary
        )

        await interaction.response.edit_message(
            content=(
                "**AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    @discord.ui.button(
        label="Background Blur",
        style=discord.ButtonStyle.secondary
    )
    async def blur_button(self, interaction, button):

        self.options["blur"] = not self.options["blur"]

        button.style = (
            discord.ButtonStyle.success
            if self.options["blur"]
            else discord.ButtonStyle.secondary
        )

        await interaction.response.edit_message(
            content=(
                "**AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    @discord.ui.button(
        label="Remove Non-Speech",
        style=discord.ButtonStyle.secondary
    )
    async def silence_button(self, interaction, button):

        self.options["remove_silence"] = not self.options["remove_silence"]

        button.style = (
            discord.ButtonStyle.success
            if self.options["remove_silence"]
            else discord.ButtonStyle.secondary
        )

        await interaction.response.edit_message(
            content=(
                "**AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    @discord.ui.button(
        label="Split Screen",
        style=discord.ButtonStyle.secondary,
        row=4
    )
    async def split_screen_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        try:
            await interaction.user.send(
                "Split Screen Background\n\n"
                "Upload the IMAGE or VIDEO you want to use "
                "for the bottom half of your edit.\n\n"
                "Accepted: JPG, JPEG, PNG, WEBP, GIF, MP4, MOV, "
                "WEBM, MKV, AVI, M4V\n\n"
                "You have 5 minutes."
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I cannot DM you. Enable DMs from this server and try again.",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Check your DMs and upload the Split Screen image or video.",
            ephemeral=True
        )

        def split_check(message):
            return (
                message.author.id == interaction.user.id
                and isinstance(message.channel, discord.DMChannel)
                and len(message.attachments) > 0
            )

        try:
            reply = await client.wait_for(
                "message",
                timeout=300,
                check=split_check
            )
        except asyncio.TimeoutError:
            await interaction.user.send(
                "Split Screen upload timed out. Press the button again."
            )
            return

        attachment = reply.attachments[0]
        suffix = Path(attachment.filename).suffix.lower()

        allowed_extensions = {
            ".jpg", ".jpeg", ".png", ".webp", ".gif",
            ".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"
        }

        if suffix not in allowed_extensions:
            await interaction.user.send(
                "Unsupported file. Please upload an image or video."
            )
            return

        split_dir = OUTPUT_DIR / "split_backgrounds"
        split_dir.mkdir(parents=True, exist_ok=True)

        saved_file = split_dir / (
            f"split_{interaction.user.id}_{attachment.id}{suffix}"
        )

        try:
            await attachment.save(saved_file)
        except Exception as e:
            print(f"Split Screen upload error: {e}")
            await interaction.user.send(
                "I could not save that file. Please try again."
            )
            return

        self.split_photo_file = saved_file
        self.options["split_screen"] = True

        button.style = discord.ButtonStyle.success
        button.label = "Split Screen ON"

        await interaction.user.send(
            f"Split Screen background selected: {attachment.filename}"
        )

        try:
            await interaction.message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(
        label="Auto Captions",
        style=discord.ButtonStyle.secondary,
        row=4
    )
    async def captions_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        self.options["auto_captions"] = not self.options["auto_captions"]

        if self.options["auto_captions"]:
            button.style = discord.ButtonStyle.success
            button.label = "Auto Captions ON"
        else:
            button.style = discord.ButtonStyle.secondary
            button.label = "Auto Captions"

        await interaction.response.edit_message(
            content=(
                "**AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    @discord.ui.button(
        label="Start Edit",
        style=discord.ButtonStyle.success,
        row=3
    )
    async def start_edit(self, interaction, button):

        if self.started:
            await interaction.response.send_message(
                "â³ This edit has already started.",
                ephemeral=True
            )
            return

        self.started = True
        button.disabled = True

        await interaction.response.edit_message(view=self)

        notify = (
            self.channel.send
            if self.channel
            else interaction.followup.send
        )

        if not self.file_path.exists():

            await notify(
                "âŒ **This edit panel has expired.**\n\n"
                "The downloaded source clip for this session is "
                "no longer on disk.\n\n"
                "Please run `!edit` again with your clip link."
            )

            SESSIONS.pop(self.user.id, None)
            return

        output_file = OUTPUT_DIR / (
            f"edited_{self.user.id}_{self.file_path.stem}.mp4"
        )

        temp_files = []

        try:

            # -------------------------------------------------
            # Keep the original full-length video by default.
            # -------------------------------------------------

            working_file = self.file_path

            # -------------------------------------------------
            # Remove non-speech only when enabled.
            # -------------------------------------------------

            if self.options["remove_silence"]:

                silence_file = OUTPUT_DIR / (
                    f"silence_removed_{self.user.id}_{self.file_path.stem}.mp4"
                )

                await notify(
                    "Removing silent/non-speaking sections..."
                )

                await run_encode_job(
                    remove_silence,
                    working_file,
                    silence_file
                )

                working_file = silence_file
                temp_files.append(silence_file)

            # -------------------------------------------------
            # Final video editing.
            # -------------------------------------------------
            # -------------------------------------------------

            await notify(
                "ðŸŽ¬ Editing video...\n"
                f"**{self.summary()}**"
            )

            await run_encode_job(
                edit_video,
                working_file,
                output_file,
                self.options,
                self.overlay_file,
                self.url_streamer_hint,
                self.split_photo_file
            )

            if not output_file.exists():
                raise RuntimeError(
                    "FFmpeg finished but output file was not created."
                )

            # -------------------------------------------------
            # ALWAYS APPLY WORD-BY-WORD CAPTIONS
            # -------------------------------------------------

            final_delivery_file = output_file

            captioned_file = OUTPUT_DIR / (
                f"captioned_{self.user.id}_{self.file_path.stem}.mp4"
            )

            await notify(
                "Adding automatic word-by-word captions...\n"
                "Whisper is transcribing the edited video."
            )

            try:

                await run_encode_job(
                    burn_word_captions,
                    output_file,
                    captioned_file
                )

                if not captioned_file.exists():
                    raise RuntimeError(
                        "Caption process finished but the captioned "
                        "video was not created."
                    )

                final_delivery_file = captioned_file

                try:
                    output_file.unlink(missing_ok=True)
                except Exception:
                    pass

                caption_size_mb = (
                    final_delivery_file.stat().st_size
                    / (1024 * 1024)
                )

                print(
                    f"Captioned output size: "
                    f"{caption_size_mb:.2f} MB"
                )

                await notify(
                    "Auto captions finished successfully."
                )

            except Exception as caption_error:

                print(
                    "Automatic captioning failed:",
                    caption_error
                )

                traceback.print_exc()

                try:
                    captioned_file.unlink(missing_ok=True)
                except Exception:
                    pass

                final_delivery_file = output_file

                await notify(
                    "Auto captioning failed, so I will deliver "
                    "the normal edited video instead."
                )

            # -------------------------------------------------
            # DISCORD DELIVERY
            # -------------------------------------------------


            output_size_mb = (
                final_delivery_file.stat().st_size
                / (1024 * 1024)
            )

            print(
                f"Final output size: {output_size_mb:.2f} MB"
            )

            try:
                final_size = final_delivery_file.stat().st_size if Path(final_delivery_file).exists() else 0
                if final_size > 25 * 1024 * 1024:
                    raise discord.HTTPException(
                        response=None,
                        data={'code': 40005, 'message': 'Request entity too large'}
                    )

                await self.user.send(
                    "✅ **Your edited KICK clip is ready!**",
                    file=discord.File(
                        str(final_delivery_file),
                        filename="edited_kick_clip.mp4"
                    ),
                    view=CaptionView(
                        final_delivery_file,
                        self.url_streamer_hint
                    )
                )

                await notify(
                    "✅ Done! I sent the edited KICK clip "
                    "to your **DM**."
                )

            except discord.HTTPException as upload_error:

                print(
                    "Discord direct upload failed:",
                    upload_error
                )

                await notify(
                    "ðŸŒ Discord cannot send this video directly. "
                    "Creating your temporary Cloudflare "
                    "download link..."
                )

                download_url = await create_public_download_link(
                    str(final_delivery_file)
                )

                if download_url:

                    try:

                        await self.user.send(
                            "âœ… **Your edited KICK clip is ready!**\n\n"
                            "Discord could not send the video "
                            "directly because it is too large.\n\n"
                            "ðŸ”— **Download your original edited video:**\n"
                            f"{download_url}\n\n"
                            "â³ **This link expires in 60 minutes.**"
                        )

                        await notify(
                            "âœ… Done! I sent the temporary "
                            "**Cloudflare download link** "
                            "to your DM.\n\n"
                            "â³ It expires in 60 minutes."
                        )

                    except discord.HTTPException as dm_error:

                        print(
                            "Could not DM Cloudflare link:",
                            dm_error
                        )

                        await notify(
                            "âš ï¸ I created the Cloudflare download "
                            "link, but I couldn't send you a DM.\n\n"
                            f"ðŸ”— {download_url}"
                        )

                else:

                    await notify(
                        "âŒ **The edit finished, but I couldn't "
                        "deliver the video.**\n\n"
                        f"Final file size: **{output_size_mb:.1f} MB**\n\n"
                        "Discord rejected the direct upload and "
                        "the Cloudflare download link could not "
                        "be created."
                    )

        except Exception as e:

            print("\nEDIT ERROR:", e)
            traceback.print_exc()

            try:

                await notify(
                    f"âŒ **EDIT FAILED**\n```{str(e)[:1500]}```"
                )

            except Exception as notify_error:

                print(
                    "Also failed to notify the channel:",
                    notify_error
                )

        finally:

            # Delete temporary intermediate files.

            for temp_file in temp_files:

                try:
                    Path(temp_file).unlink(
                        missing_ok=True
                    )

                except Exception:
                    pass

            # Delete uploaded Split Screen photo.

            if self.split_photo_file:

                try:
                    Path(
                        self.split_photo_file
                    ).unlink(
                        missing_ok=True
                    )

                except Exception:
                    pass

            # Delete downloaded KICK source and job directory.

            cleanup_job_files(
                self.file_path
            )

            # Remove active session.

            SESSIONS.pop(
                self.user.id,
                None
            )


            # ---------------------------------------------------------
            # Bot ready
            # ---------------------------------------------------------

@client.event
async def on_ready():

    print("----------------------------------")
    print(f"Logged in as {client.user}")
    print("Auto Edit Bot is ready!")
    print("----------------------------------")

    named_overlays = get_overlay_library()

    if named_overlays:
        print(
            "Named overlays detected:",
            ", ".join(
                sorted(named_overlays.keys())
            )
        )
    else:
        print(
            "WARNING: No named overlays found in overlays folder."
        )

    print("----------------------------------")


# ---------------------------------------------------------
# VOD Intelligence Pipeline Handler
# ---------------------------------------------------------

async def handle_vod_command(message, content):
    """Handle !vod command for full VOD analysis."""
    job_manager = get_job_manager()

    # Parse command: !vod <url> or !vod
    parts = content.strip().split(maxsplit=1)

    if len(parts) < 2:
        await message.channel.send(
            "**VOD Intelligence Pipeline**\n\n"
            "Usage: `!vod <KICK VOD URL>`\n\n"
            "Example:\n"
            "`!vod https://kick.com/username/videos/12345678`\n\n"
            "This will:\n"
            "1. Download the full VOD\n"
            "2. Analyze audio, transcript, and chat\n"
            "3. Find viral-worthy moments with context\n"
            "4. Rank and deduplicate clips\n"
            "5. Present top clips for review/edit"
        )
        return

    vod_url = parts[1].strip()

    # Validate VOD URL
    if not is_kick_vod_url(vod_url):
        await message.channel.send(
            "❌ That doesn't look like a KICK VOD URL.\n\n"
            "VOD URLs look like: `https://kick.com/username/videos/12345678`\n"
            "(Not clip URLs which are `/clip/` or `/clips/`)"
        )
        return

    user_id = message.author.id

    # Check for existing job
    existing_jobs = job_manager.get_user_jobs(user_id)
    active_jobs = [j for j in existing_jobs if j.state not in (JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED)]
    if active_jobs:
        await message.channel.send(
            f"⏳ You already have an active VOD job ({active_jobs[0].id}). "
            "Please wait for it to complete or cancel it first."
        )
        return

    # Create job
    job = job_manager.create_job(vod_url, user_id, message.channel.id)

    status_msg = await message.channel.send(
        f"🚀 **Starting VOD Analysis**\n"
        f"Job ID: `{job.id}`\n"
        f"URL: {vod_url}\n\n"
        f"📥 Downloading VOD..."
    )

    # Run pipeline in background
    async def run_pipeline():
        try:
            ranking_result = await run_vod_pipeline(job.id, job_manager)

            # Get metadata
            from vod_intelligence import MetadataGenerator
            metadata_gen = MetadataGenerator()

            # Load transcript for metadata
            from vod_intelligence.transcript_analyzer import TranscriptAnalyzer
            transcript_analyzer = TranscriptAnalyzer()
            transcript = transcript_analyzer.load_analysis(
                Path(job_manager.get_job(job.id).transcript_path or job_manager.get_job(job.id).analysis_path)
            )

            metadata_list = await generate_metadata_for_all_clips(
                ranking_result.top_clips, transcript, job.streamer_name, job.vod_title, vod_url
            )
            metadata_map = {f"rank_{m.rank}": m for m in metadata_list}

            # Update status
            await status_msg.edit(
                content=(
                    f"✅ **VOD Analysis Complete!**\n"
                    f"Job: `{job.id}` | Streamer: **{job.streamer_name}**\n"
                    f"Duration: {job.vod_duration/3600:.1f}h\n"
                    f"Candidates: {ranking_result.total_candidates} | Top: {len(ranking_result.top_clips)} | Dupes: {ranking_result.duplicates_removed}\n\n"
                    f"🎬 **Top Clips Ready for Review**"
                )
            )

            if not ranking_result.top_clips:
                await message.channel.send(
                    "⚠️ No reviewable clips were found after ranking. "
                    "Try a different VOD or check back later."
                )
                return

            # Start review session
            async def edit_callback(interaction, job, clip, options):
                await process_vod_clip_edit(interaction, job, clip, options, metadata_map)

            await start_review_session(
                interaction=None,  # We'll send new message
                job=job,
                ranking_result=ranking_result,
                metadata_map=metadata_map,
                job_manager=job_manager,
                edit_callback=edit_callback
            )

            # Send review interface
            from vod_intelligence.review_interface import build_review_list_embed, ClipReviewView

            async def on_edit(interaction, job, clip, options):
                await process_vod_clip_edit(interaction, job, clip, options, metadata_map)

            async def on_reject(interaction, clip):
                job.deselect_clip(f"rank_{clip.rank}")
                await interaction.followup.send(f"❌ Rejected clip #{clip.rank}", ephemeral=True)

            async def on_edit_all(interaction, clips):
                for c in clips:
                    job.select_clip(f"rank_{c.rank}")
                await process_vod_clip_edit(interaction, job, clips, {}, metadata_map)

            view = ClipReviewView(
                job=job,
                ranking_result=ranking_result,
                metadata_map=metadata_map,
                job_manager=job_manager,
                on_edit=on_edit,
                on_reject=on_reject,
                on_edit_all=on_edit_all
            )

            embed = build_review_list_embed(job, ranking_result)

            review_msg = await message.channel.send(
                content=(
                    f"🎬 **Edit Options for Job `{job.id}`**\n"
                    f"Use the buttons below to review, edit, or reject clips.\n"
                    f"Top clips: **{len(ranking_result.top_clips)}**"
                ),
                embed=embed,
                view=view
            )

            await message.channel.send(
                "👇 **Select a clip above, then choose an edit option.**\n"
                "• Click a clip number to open its edit menu\n"
                "• Use **Edit All** to process all top clips\n"
                "• Results will be sent to your DM."
            )

        except Exception as e:
            traceback.print_exc()
            await status_msg.edit(
                content=f"❌ **VOD Analysis Failed**\nJob: `{job.id}`\nError: {str(e)[:1800]}"
            )

    # Start background task
    asyncio.create_task(run_pipeline())


async def process_vod_clip_edit(interaction, job, clip_or_clips, options, metadata_map):
    """Process editing of selected VOD clip(s)."""
    from vod_intelligence.edit_planner import EditPlanner
    from vod_intelligence.clip_ranker import RankedClip
    from vod_intelligence.transcript_analyzer import TranscriptAnalyzer

    try:
        await interaction.response.defer()
    except Exception:
        pass

    clips = clip_or_clips if isinstance(clip_or_clips, list) else [clip_or_clips]

    # Load transcript from job analysis
    transcript = None
    if job.transcript_path:
        try:
            transcript = TranscriptAnalyzer().load_analysis(Path(job.transcript_path))
        except Exception:
            pass

    for clip in clips:
        metadata = metadata_map.get(f"rank_{clip.rank}")

        if metadata is None:
            try:
                from vod_intelligence.metadata_generator import MetadataGenerator, ClipMetadata
                key_info = {
                    "streamer": job.streamer_name or "Streamer",
                    "start_time": clip.moment.expanded_start,
                    "entities": [],
                    "topics": [],
                    "key_phrases": [],
                    "emotions": [],
                }
                metadata = MetadataGenerator()._generate_template_fallback(key_info)
                metadata.rank = clip.rank
            except Exception:
                metadata = None

        # Create edit plan
        planner = EditPlanner()
        plan = planner.create_plan(
            clip.moment, clip.moment_score, clip.story_analysis,
            transcript, job.streamer_name, job.vod_title
        )

        # Apply user customizations
        for key, value in options.items():
            if hasattr(plan, key):
                setattr(plan, key, value)

        edit_options = plan.to_options_dict()

        # Run edit using existing edit_video function
        output_file = Path("output") / f"vod_{job.id}_clip{clip.rank}_{clip.moment.expanded_start:.0f}.mp4"

        try:
            await run_encode_job(
                edit_video,
                Path(job.vod_path),
                output_file,
                edit_options,
                None,  # overlay_file - will auto-detect
                job.streamer_name,
                None  # split_photo_file
            )

            # Add captions if requested
            if edit_options.get("auto_captions"):
                captioned_file = Path("output") / f"vod_{job.id}_clip{clip.rank}_captioned.mp4"
                await run_encode_job(
                    burn_word_captions,
                    output_file,
                    captioned_file
                )
                output_file.unlink(missing_ok=True)
                output_file = captioned_file

            # Send to user
            file_size = output_file.stat().st_size if Path(output_file).exists() else 0
            if file_size > 25 * 1024 * 1024:
                download_url = await create_public_download_link(
                    str(output_file)
                )
                if download_url:
                    caption_display = metadata.recommended_post_caption if metadata and metadata.recommended_post_caption else (metadata.caption if metadata else 'N/A')
                    await interaction.user.send(
                        f"✅ **Clip #{clip.rank} Ready!**\n"
                        f"Title: {metadata.title if metadata else 'Untitled'}\n"
                        f"Caption: {caption_display}\n"
                        f"Hashtags: {' '.join(metadata.hashtags[:10]) if metadata else 'N/A'}\n\n"
                        "Discord could not send the video directly because it is too large.\n\n"
                        f"🔗 **Download link:**\n{download_url}\n\n"
                        "⏳ **This link expires in 60 minutes.**"
                    )
                else:
                    caption_display = metadata.recommended_post_caption if metadata and metadata.recommended_post_caption else (metadata.caption if metadata else 'N/A')
                    await interaction.user.send(
                        f"✅ **Clip #{clip.rank} Ready!**\n"
                        f"Title: {metadata.title if metadata else 'Untitled'}\n"
                        f"Caption: {caption_display}\n"
                        f"Hashtags: {' '.join(metadata.hashtags[:10]) if metadata else 'N/A'}"
                    )
            else:
                try:
                    caption_display = metadata.recommended_post_caption if metadata and metadata.recommended_post_caption else (metadata.caption if metadata else 'N/A')
                    await interaction.user.send(
                        f"✅ **Clip #{clip.rank} Ready!**\n"
                        f"Title: {metadata.title if metadata else 'Untitled'}\n"
                        f"Caption: {caption_display}\n"
                        f"Hashtags: {' '.join(metadata.hashtags[:10]) if metadata else 'N/A'}",
                        file=discord.File(str(output_file), filename=f"clip_{clip.rank}.mp4")
                    )
                except discord.HTTPException as send_error:
                    if send_error.status == 413:
                        download_url = await create_public_download_link(str(output_file))
                        if download_url:
                            caption_display = metadata.recommended_post_caption if metadata and metadata.recommended_post_caption else (metadata.caption if metadata else 'N/A')
                            await interaction.user.send(
                                f"✅ **Clip #{clip.rank} Ready!**\n"
                                f"Title: {metadata.title if metadata else 'Untitled'}\n"
                                f"Caption: {caption_display}\n"
                                f"Hashtags: {' '.join(metadata.hashtags[:10]) if metadata else 'N/A'}\n\n"
                                "Discord could not send the video directly because it is too large.\n\n"
                                f"🔗 **Download link:**\n{download_url}\n\n"
                                "⏳ **This link expires in 60 minutes.**"
                            )
                        else:
                            caption_display = metadata.recommended_post_caption if metadata and metadata.recommended_post_caption else (metadata.caption if metadata else 'N/A')
                            await interaction.user.send(
                                f"✅ **Clip #{clip.rank} Ready!**\n"
                                f"Title: {metadata.title if metadata else 'Untitled'}\n"
                                f"Caption: {caption_display}\n"
                                f"Hashtags: {' '.join(metadata.hashtags[:10]) if metadata else 'N/A'}"
                            )
                    else:
                        raise

            # Record in history
            record_clip(
                output_file,
                streamer=job.streamer_name,
                source_url=job.vod_url,
                start_time=clip.moment.expanded_start,
                end_time=clip.moment.expanded_end,
                transcript=clip.moment.hook_text + " " + clip.moment.payoff_text,
                score=clip.final_score
            )

            await interaction.followup.send(f"✅ Clip #{clip.rank} sent to your DM!")

        except Exception as e:
            traceback.print_exc()
            await interaction.followup.send(f"❌ Failed to edit clip #{clip.rank}: {str(e)[:500]}")


# ---------------------------------------------------------
# Clip URL Modal - Opens a modal for pasting KICK clip URLs
# ---------------------------------------------------------

class ClipURLModal(discord.ui.Modal, title="KICK Clip Editor"):
    """Modal that opens when user clicks the button - paste URL and edit options."""

    def __init__(self):
        super().__init__()
        self.url_input = discord.ui.TextInput(
            label="KICK Clip URL",
            placeholder="https://kick.com/username/clips/12345678",
            style=discord.InputStyle.short,
            required=True,
            min_length=10,
        )
        self.add_item(self.url_input)

        self.size_input = discord.ui.TextInput(
            label="Output format (9:16 / 1:1 / 4:5 / Original)",
            placeholder="9:16",
            style=discord.InputStyle.short,
            required=False,
            default="9:16",
        )
        self.add_item(self.size_input)

        self.captions_input = discord.ui.TextInput(
            label="Captions? (yes/no)",
            placeholder="yes",
            style=discord.InputStyle.short,
            required=False,
            default="yes",
        )
        self.add_item(self.captions_input)

    async def callback(self, interaction: discord.Interaction):
        clip_url = self.url_input.value.strip()

        if not is_kick_clip_url(clip_url):
            await interaction.response.send_message(
                "❌ That doesn't look like a KICK Clip URL.",
                ephemeral=True
            )
            return

        user_id = interaction.user.id
        job_manager = get_job_manager()

        # Check for existing job
        existing_jobs = job_manager.get_user_jobs(user_id)
        active_jobs = [j for j in existing_jobs if j.state not in (JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED)]
        if active_jobs:
            await interaction.response.send_message(
                f"⏳ You already have an active job ({active_jobs[0].id}).",
                ephemeral=True
            )
            return

        # Create job
        job = job_manager.create_job(clip_url, user_id, interaction.channel.id, ContentType.CLIP)

        await interaction.response.send_message(
            f"🚀 **Starting Clip Analysis**\n"
            f"Job ID: `{job.id}`\n"
            f"URL: {clip_url}\n\n"
            f"📥 Downloading clip...",
            ephemeral=True
        )

        status_msg = await interaction.channel.send(
            f"🚀 **Starting Clip Analysis**\n"
            f"Job ID: `{job.id}`\n"
            f"URL: {clip_url}\n\n"
            f"📥 Downloading clip..."
        )

        async def run_pipeline():
            try:
                file_path = await run_download_job(
                    download_kick_clip,
                    clip_url,
                    user_id
                )
                duration = get_video_duration(file_path)
                streamer = extract_streamer_from_url(clip_url) or "unknown"

                job.vod_path = str(file_path)
                job.vod_duration = duration
                job.streamer_name = streamer
                job.vod_title = f"Clip from {streamer}"
                job_manager.set_vod_info(job.id, str(file_path), duration, f"Clip from {streamer}", streamer)

                ranking_result = await run_clip_pipeline(job.id, job_manager)

                from vod_intelligence.review_interface import build_review_list_embed, ClipReviewView
                from vod_intelligence.transcript_analyzer import TranscriptAnalyzer
                transcript_analyzer = TranscriptAnalyzer()
                transcript = transcript_analyzer.load_analysis(
                    Path(job_manager.get_job(job.id).transcript_path or job_manager.get_job(job.id).analysis_path)
                )

                metadata_list = await generate_metadata_for_all_clips(
                    ranking_result.top_clips, transcript, streamer, job.vod_title, clip_url
                )
                metadata_map = {f"rank_{m.rank}": m for m in metadata_list}

                await status_msg.edit(
                    content=(
                        f"✅ **Clip Analysis Complete!**\n"
                        f"Job: `{job.id}` | Streamer: **{streamer}**\n"
                        f"Duration: {duration:.1f}s\n\n"
                        f"🎬 **Review Edit Recommendations**"
                    )
                )

                if not ranking_result.top_clips:
                    await interaction.channel.send(
                        "⚠️ No reviewable clips were found after ranking. "
                        "Try a different clip or check back later."
                    )
                    return

                async def on_edit(interaction, job, clip, options):
                    await process_vod_clip_edit(interaction, job, clip, options, metadata_map)

                async def on_reject(interaction, clip):
                    job.deselect_clip(f"rank_{clip.rank}")
                    await interaction.followup.send(f"❌ Rejected clip #{clip.rank}", ephemeral=True)

                async def on_edit_all(interaction, clips):
                    for c in clips:
                        job.select_clip(f"rank_{c.rank}")
                    await process_vod_clip_edit(interaction, job, clips, {}, metadata_map)

                view = ClipReviewView(
                    job=job,
                    ranking_result=ranking_result,
                    metadata_map=metadata_map,
                    job_manager=job_manager,
                    on_edit=on_edit,
                    on_reject=on_reject,
                    on_edit_all=on_edit_all
                )

                embed = build_review_list_embed(job, ranking_result)

                review_msg = await interaction.channel.send(
                    content=(
                        f"🎬 **Edit Options for Job `{job.id}`**\n"
                        f"Use the buttons below to review, edit, or reject clips.\n"
                        f"Top clips: **{len(ranking_result.top_clips)}**"
                    ),
                    embed=embed,
                    view=view
                )

                await interaction.channel.send(
                    "👇 **Select a clip above, then choose an edit option.**\n"
                    "• Click a clip number to open its edit menu\n"
                    "• Use **Edit All** to process all top clips\n"
                    "• Results will be sent to your DM."
                )

            except Exception as e:
                traceback.print_exc()
                await status_msg.edit(
                    content=f"❌ **Clip Analysis Failed**\nJob: `{job.id}`\nError: {str(e)[:1800]}"
                )

        asyncio.create_task(run_pipeline())


class ClipURLModalView(discord.ui.View):
    """Persistent view with a button to open the clip URL modal."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔗 Paste KICK Clip URL", style=discord.ButtonStyle.primary, custom_id="kick_clip_url")
    async def open_modal(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            modal = ClipURLModal()
            await interaction.response.send_modal(modal)
        except Exception as e:
            print(f"Modal error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Error: {e}", ephemeral=True)


# Register persistent view so buttons work even after restart
client.add_view(ClipURLModalView())


# ---------------------------------------------------------
# Messages
# ---------------------------------------------------------

@client.event
async def on_message(message):

    if message.author == client.user:
        return

    content = message.content.strip()

    # Help command - show available channels
    if content.lower() == "!channels":
        guild = message.guild
        if guild:
            text_channels = [c for c in guild.text_channels]
            for ch in text_channels[:20]:
                await message.channel.send(f"**{ch.name}** — ID: `{ch.id}`")
        else:
            await message.channel.send("Run this in a server, not DMs.")
        return

    # Restrict !clip command to a specific channel only
    CLIP_CHANNEL_ID = os.getenv("CLIP_CHANNEL_ID")
    if CLIP_CHANNEL_ID and message.channel.id != int(CLIP_CHANNEL_ID):
        return

    # Handle !clip command - paste URL directly in command
    if content.lower().startswith("!clip"):
        await handle_clip_command(message, content)
        return

    # Handle !upload command - upload a video file
    if content.lower().startswith("!upload"):
        await handle_upload_command(message, content)
        return

    # Handle !download command - download only, no analysis
    if content.lower().startswith("!download"):
        await handle_download_command(message, content)
        return

    # Cancel command
    if content.lower() == "!cancel":
        job_manager = get_job_manager()
        user_jobs = job_manager.get_user_jobs(message.author.id)
        active = [j for j in user_jobs if j.state not in (JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED)]
        for j in active:
            job_manager.cancel_job(j.id)
            await message.channel.send(f"❌ Cancelled job `{j.id}`")
        if not active:
            await message.channel.send("No active jobs to cancel.")
        return

    # Jobs command
    if content.lower() == "!jobs":
        job_manager = get_job_manager()
        user_jobs = job_manager.get_user_jobs(message.author.id)
        if not user_jobs:
            await message.channel.send("No jobs found.")
            return
        for j in user_jobs[:10]:
            emoji = {"COMPLETE": "✅", "FAILED": "❌", "CANCELLED": "❌", "CREATED": "🟡", "DOWNLOADING": "📥", "ANALYZING": "🔍", "CANDIDATES_FOUND": "🎬", "READY": "✅"}.get(j.state.value, "⏳")
            await message.channel.send(f"{emoji} `{j.id}` — {j.state.value} — {j.vod_title or 'Untitled'}")
        return

    if not content.lower().startswith("!edits"):
        return

    # -----------------------------------------------------
    # !edit
    #
    # The user now supplies a KICK Clip URL instead of
    # uploading a large video file to Discord.
    # -----------------------------------------------------

    kick_url = clean_url_from_message(content)

    # Support:
    # !edit https://kick.com/.../clip/...
    #
    # Also support:
    # !edit
    # followed by a URL in the next Discord message.
    if kick_url and is_kick_clip_url(kick_url):

        await start_kick_edit(
            message,
            kick_url
        )

        return

    if kick_url and not is_kick_clip_url(kick_url):

        await message.channel.send(
            "❌ I found a URL, but it does not look like a KICK Clip URL.\n\n"
            "Use:\n"
            "`!edit https://kick.com/.../clip/...`"
        )

        return

    # If there is no URL after !edit, ask the user for one.
    await message.channel.send(
        "Paste your **KICK Clip link** here.\n\n"
        "Example:\n"
        "`https://kick.com/.../clip/...`\n\n"
    )

    def check(reply):
        return (
            reply.author.id == message.author.id
            and reply.channel.id == message.channel.id
        )

    try:
        reply = await client.wait_for(
            "message",
            timeout=180,
            check=check
        )
    except asyncio.TimeoutError:

        await message.channel.send(
            "Ã¢Å’â€º Timed out. Run `!edit` again when you have a KICK Clip link."
        )

        return

    kick_url = clean_url_from_message(
        reply.content
    )

    if not kick_url or not is_kick_clip_url(kick_url):

        await message.channel.send(
            "Ã¢ÂÅ’ That isn't a valid KICK Clip link.\n\n"
            "Run `!edit` again and paste the KICK Clip URL."
        )

        return

    await start_kick_edit(
        message,
        kick_url
    )



class CaptionButtonView(discord.ui.View):
    """
    Shown alongside a finished edited clip. Generates a separate
    caption text file rather than burning captions into the video.
    """

    def __init__(self, output_file, streamer_hint):
        super().__init__(timeout=None)
        self.output_file = output_file
        self.streamer_hint = streamer_hint
        self.used = False
        self.caption_text = None
        self.caption_file = None

    @discord.ui.button(label="Generate Caption", emoji="📝", style=discord.ButtonStyle.primary)
    async def generate_caption_button(self, interaction, button):
        if self.used:
            await interaction.response.send_message(
                "This caption has already been generated.",
                ephemeral=True
            )
            return

        self.used = True
        button.disabled = True
        await interaction.response.edit_message(view=self)

        await interaction.followup.send("Generating caption, this may take a minute...")

        try:
            caption_file, caption_text = await run_encode_job(
                generate_caption_file,
                self.output_file,
                self.streamer_hint
            )

            self.caption_file = caption_file
            self.caption_text = caption_text

            await interaction.followup.send(
                f"**Caption:**\n{caption_text}"
            )

            # Send the caption file
            if caption_file and Path(caption_file).exists():
                await interaction.followup.send(
                    "📄 Caption file:",
                    file=discord.File(
                        caption_file,
                        filename=f"caption_{Path(self.output_file).stem}.txt"
                    )
                )

        except Exception as caption_error:
            print("Caption generation failed:", caption_error)
            await interaction.followup.send(
                "Sorry, caption generation failed for this clip."
            )


# Alias for backward compatibility
CaptionView = CaptionButtonView


async def generate_caption_file(video_path, streamer_name=None):
    """
    Generate a separate caption text file (not burnt into video).
    Returns the path to the caption file.
    """
    from captioning import transcribe_and_caption

    caption_text = await run_encode_job(
        transcribe_and_caption,
        video_path,
        streamer_name
    )

    caption_file = OUTPUT_DIR / f"caption_{Path(video_path).stem}.txt"
    caption_file.write_text(caption_text or "", encoding="utf-8")

    return str(caption_file), caption_text


class CaptionButtonView(discord.ui.View):
    """View with a Caption button that generates a separate caption file."""

    def __init__(self, output_file, streamer_hint=None, channel=None):
        super().__init__(timeout=3600)
        self.output_file = output_file
        self.streamer_hint = streamer_hint
        self.channel = channel
        self.used = False
        self.caption_text = None
        self.caption_file = None

    @discord.ui.button(label="Generate Caption", emoji="📝", style=discord.ButtonStyle.primary)
    async def generate_caption_button(self, interaction, button):
        if self.used:
            await interaction.response.send_message(
                "This caption has already been generated.",
                ephemeral=True
            )
            return

        self.used = True
        button.disabled = True
        await interaction.response.edit_message(view=self)

        await interaction.followup.send("Generating caption, this may take a minute...")

        try:
            caption_file, caption_text = await run_encode_job(
                generate_caption_file,
                self.output_file,
                self.streamer_hint
            )

            self.caption_file = caption_file
            self.caption_text = caption_text

            await interaction.followup.send(
                f"**Caption:**\n{caption_text}"
            )

            # Also send the caption file
            if Path(caption_file).exists():
                await interaction.followup.send(
                    f"📄 Caption file:",
                    file=discord.File(caption_file, filename=f"caption_{Path(self.output_file).stem}.txt")
                )

        except Exception as caption_error:
            print("Caption generation failed:", caption_error)
            await interaction.followup.send(
                "Sorry, caption generation failed for this clip."
            )


async def start_kick_edit(message, kick_url):

    user_id = message.author.id

    if user_id in SESSIONS:

        await message.channel.send(
            "Ã¢ÂÂ³ You already have an edit running. "
            "Please wait for it to finish."
        )

        return

    status = await message.channel.send(
        "Ã°Å¸â€œÂ¥ **Downloading your KICK Clip...**\n"
    )

    try:

        file_path = await run_download_job(
            download_kick_clip,
            kick_url,
            user_id
        )

        duration = get_video_duration(file_path)

        if duration <= 0:
            raise RuntimeError(
                "Downloaded KICK Clip has an invalid duration."
            )

        # KICK Clip URLs always contain the streamer's username as the
        # first path segment (kick.com/<username>/clips/<clip_id>).
        # We use this directly to select the overlay, which is faster
        # and more reliable than reading it off the video with OCR.
        streamer_hint = extract_streamer_from_url(kick_url)

        if streamer_hint:
            print(f"Streamer name from URL: {streamer_hint}")

        creator_line = (
            f"Creator detected from link: **{streamer_hint}**\n"
            if streamer_hint
            else ""
        )

        await status.edit(
            content=(
                "**KICK Clip downloaded!**\n"
                f"Duration: **{duration:.1f}s**\n"
                f"{creator_line}\n"
                "Choose your editing options below, then press "
                "**Start Edit**."
            )
        )

        view = EditView(
            message.author,
            file_path,
            streamer_hint,
            message.channel
        )

        SESSIONS[user_id] = view

        await message.channel.send(
            "**AUTO EDIT OPTIONS**\n\n"
            "Choose everything you want, then press **Start Edit**.\n\n"
            "Selected: **9:16 • Remove Non-Speech • Overlay**",
            view=view
        )

    except Exception as e:

        print("\nKICK DOWNLOAD ERROR:", e)

        await status.edit(
            content=(
                "Ã¢ÂÅ’ **KICK CLIP DOWNLOAD FAILED**\n\n"
                f"```{str(e)[:1800]}```"
            )
        )

        # Make sure a failed job does not leave files behind.
        cleanup_job_files(
            UPLOAD_DIR / f"kick_{user_id}" / "kick_clip.mp4"
        )

        SESSIONS.pop(user_id, None)


# ---------------------------------------------------------
# Start
# ---------------------------------------------------------

async def handle_clip_command(message, content):
    """Handle !clip command for short clip analysis."""
    job_manager = get_job_manager()

    parts = content.strip().split(maxsplit=1)

    if len(parts) < 2:
        await message.channel.send(
            "**Clip Intelligence**\n\n"
            "Usage: `!clip <KICK Clip URL>`\n\n"
            "Example:\n"
            "`!clip https://kick.com/username/clips/12345678`\n\n"
            "This analyzes the clip with the intelligence pipeline and "
            "presents edit recommendations."
        )
        return

    clip_url = parts[1].strip()

    # Validate clip URL
    if not is_kick_clip_url(clip_url):
        await message.channel.send(
            "❌ That doesn't look like a KICK Clip URL.\n\n"
            "Clip URLs look like: `https://kick.com/username/clips/12345678`"
        )
        return

    user_id = message.author.id

    # Check for existing job
    existing_jobs = job_manager.get_user_jobs(user_id)
    active_jobs = [j for j in existing_jobs if j.state not in (JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED)]
    if active_jobs:
        await message.channel.send(
            f"⏳ You already have an active job ({active_jobs[0].id}). "
            "Please wait for it to complete or cancel it first."
        )
        return

    # Create job
    job = job_manager.create_job(clip_url, user_id, message.channel.id, ContentType.CLIP)

    status_msg = await message.channel.send(
        f"🚀 **Starting Clip Analysis**\n"
        f"Job ID: `{job.id}`\n"
        f"URL: {clip_url}\n\n"
        f"📥 Downloading clip..."
    )

    async def run_pipeline():
        try:
            from vod_intelligence.review_interface import build_review_list_embed, ClipReviewView

            # Download clip first
            file_path = await run_download_job(
                download_kick_clip,
                clip_url,
                user_id
            )
            duration = get_video_duration(file_path)
            streamer = extract_streamer_from_url(clip_url) or "unknown"

            job.vod_path = str(file_path)
            job.vod_duration = duration
            job.streamer_name = streamer
            job.vod_title = f"Clip from {streamer}"
            job_manager.set_vod_info(job.id, str(file_path), duration, f"Clip from {streamer}", streamer)

            # Run clip pipeline
            ranking_result = await run_clip_pipeline(job.id, job_manager)

            # Load transcript for metadata
            from vod_intelligence.transcript_analyzer import TranscriptAnalyzer
            transcript_analyzer = TranscriptAnalyzer()
            transcript = transcript_analyzer.load_analysis(
                Path(job_manager.get_job(job.id).transcript_path or job_manager.get_job(job.id).analysis_path)
            )

            metadata_list = await generate_metadata_for_all_clips(
                ranking_result.top_clips, transcript, streamer, job.vod_title, clip_url
            )
            metadata_map = {f"rank_{m.rank}": m for m in metadata_list}

            await status_msg.edit(
                content=(
                    f"✅ **Clip Analysis Complete!**\n"
                    f"Job: `{job.id}` | Streamer: **{streamer}**\n"
                    f"Duration: {duration:.1f}s\n\n"
                    f"🎬 **Review Edit Recommendations**"
                )
            )

            if not ranking_result.top_clips:
                await message.channel.send(
                    "⚠️ No reviewable clips were found after ranking. "
                    "Try a different clip or check back later."
                )
                return

            async def on_edit(interaction, job, clip, options):
                await process_vod_clip_edit(interaction, job, clip, options, metadata_map)

            async def on_reject(interaction, clip):
                job.deselect_clip(f"rank_{clip.rank}")
                await interaction.followup.send(f"❌ Rejected clip #{clip.rank}", ephemeral=True)

            async def on_edit_all(interaction, clips):
                for c in clips:
                    job.select_clip(f"rank_{c.rank}")
                await process_vod_clip_edit(interaction, job, clips, {}, metadata_map)

            view = ClipReviewView(
                job=job,
                ranking_result=ranking_result,
                metadata_map=metadata_map,
                job_manager=job_manager,
                on_edit=on_edit,
                on_reject=on_reject,
                on_edit_all=on_edit_all
            )

            embed = build_review_list_embed(job, ranking_result)

            review_msg = await message.channel.send(
                content=(
                    f"🎬 **Edit Options for Job `{job.id}`**\n"
                    f"Use the buttons below to review, edit, or reject clips.\n"
                    f"Top clips: **{len(ranking_result.top_clips)}**"
                ),
                embed=embed,
                view=view
            )

            await message.channel.send(
                "👇 **Select a clip above, then choose an edit option.**\n"
                "• Click a clip number to open its edit menu\n"
                "• Use **Edit All** to process all top clips\n"
                "• Results will be sent to your DM."
            )

        except Exception as e:
            traceback.print_exc()
            await status_msg.edit(
                content=f"❌ **Clip Analysis Failed**\nJob: `{job.id}`\nError: {str(e)[:1800]}"
            )

    asyncio.create_task(run_pipeline())


async def handle_download_command(message, content):
    """Handle !download command - download only, no analysis."""
    parts = content.strip().split(maxsplit=1)

    if len(parts) < 2:
        await message.channel.send(
            "**Download Clip**\n\n"
            "Usage: `!download <KICK Clip URL>`\n\n"
            "Example:\n"
            "`!download https://kick.com/username/clips/12345678`\n\n"
            "This downloads the clip and sends it to your DM.\n"
            "Large clips will get a temporary download link."
        )
        return

    clip_url = parts[1].strip()

    if not is_kick_clip_url(clip_url):
        await message.channel.send(
            "❌ That doesn't look like a KICK Clip URL.\n\n"
            "Clip URLs look like: `https://kick.com/username/clips/12345678`"
        )
        return

    status = await message.channel.send(
        "📥 **Downloading your KICK Clip...**\n"
    )

    try:
        file_path = await run_download_job(
            download_kick_clip,
            clip_url,
            message.author.id
        )

        duration = get_video_duration(file_path)
        if duration <= 0:
            raise RuntimeError("Downloaded clip has an invalid duration.")

        file_size = Path(file_path).stat().st_size
        size_mb = file_size / (1024 * 1024)

        await status.edit(
            content=(
                f"✅ **Clip downloaded!**\n"
                f"Duration: **{duration:.1f}s**\n"
                f"Size: **{size_mb:.1f}MB**\n\n"
                f"Sending to your DM..."
            )
        )

        # Always try direct send first, fall back to Cloudflare on 413
        try:
            await message.author.send(
                f"✅ **Your KICK Clip is ready!**\n"
                f"Duration: {duration:.1f}s\n"
                f"Size: {size_mb:.1f}MB",
                file=discord.File(file_path, filename=f"kick_clip_{Path(file_path).stem}.mp4")
            )
        except discord.HTTPException as dm_error:
            if dm_error.status == 413:
                download_url = await create_public_download_link(file_path)
                if download_url:
                    await message.author.send(
                        f"✅ **Your KICK Clip is ready!**\n"
                        f"Duration: {duration:.1f}s\n"
                        f"Size: {size_mb:.1f}MB\n\n"
                        "Discord could not send the video directly because it is too large.\n\n"
                        f"🔗 **Download link:**\n{download_url}\n\n"
                        "⏳ **This link expires in 60 minutes.**"
                    )
                else:
                    await message.author.send(
                        f"✅ **Your KICK Clip is ready!**\n"
                        f"Duration: {duration:.1f}s\n"
                        f"Size: {size_mb:.1f}MB\n\n"
                        "Could not create a download link. "
                        "Try downloading the clip directly from KICK."
                    )
            else:
                raise

        await status.edit(
            content=(
                f"✅ **Clip sent to your DM!**\n"
                f"Duration: **{duration:.1f}s**\n"
                f"Size: **{size_mb:.1f}MB**"
            )
        )

    except Exception as e:
        print("\nDOWNLOAD ERROR:", e)
        await status.edit(
            content=(
                "❌ **DOWNLOAD FAILED**\n\n"
                f"```{str(e)[:1800]}```\n\n"
                "Make sure the Clip opens normally in your browser and "
                "that yt-dlp/curl_cffi are installed."
            )
        )
        cleanup_job_files(UPLOAD_DIR / f"kick_{message.author.id}" / "kick_clip.mp4")


async def handle_upload_command(message, content):
    """Handle !upload command - upload a video file via web."""
    UPLOAD_CHANNEL_ID = int(os.getenv("UPLOAD_CHANNEL_ID", "1547844146822127626"))
    if message.channel.id != UPLOAD_CHANNEL_ID:
        await message.channel.send(
            f"❌ This command only works in the upload channel.\n"
            f"Channel ID: `{UPLOAD_CHANNEL_ID}`"
        )
        return

    await message.channel.send(
        "📤 **Upload your video**\n\n"
        "Click the button below to open the upload page.\n\n"
        "After uploading, you'll get a download link here.",
        view=UploadView()
    )


class UploadView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="📤 Open Upload Page",
        style=discord.ButtonStyle.primary,
        custom_id="open_upload_page"
    )
    async def open_upload(self, interaction: discord.Interaction, button: discord.ui.Button):
        upload_url = get_upload_public_url() or os.getenv("UPLOAD_SERVER_URL", "http://localhost:8766")
        await interaction.response.send_message(
            f"📤 **Upload your video here:**\n{upload_url}\n\n"
            "After uploading, the download link will appear here automatically.",
            ephemeral=True
        )


async def handle_live_command(message, content):
    """Handle !live command for live stream analysis."""
    job_manager = get_job_manager()

    parts = content.strip().split(maxsplit=1)

    if len(parts) < 2:
        await message.channel.send(
            "**Live Stream Intelligence**\n\n"
            "Usage: `!live <KICK Stream URL>`\n\n"
            "Example:\n"
            "`!live https://kick.com/username`\n\n"
            "This buffers the live stream and analyzes it for viral moments."
        )
        return

    stream_url = parts[1].strip()

    # Validate stream URL
    try:
        from urllib.parse import urlparse
        parsed = urlparse(stream_url.strip())
        host = (parsed.hostname or "").lower()
        if host not in {"kick.com", "www.kick.com"}:
            await message.channel.send(
                "❌ That doesn't look like a KICK stream URL.\n\n"
                "Stream URLs look like: `https://kick.com/username`"
            )
            return
    except Exception:
        await message.channel.send("❌ Invalid URL format.")
        return

    user_id = message.author.id

    # Check for existing job
    existing_jobs = job_manager.get_user_jobs(user_id)
    active_jobs = [j for j in existing_jobs if j.state not in (JobState.COMPLETE, JobState.FAILED, JobState.CANCELLED)]
    if active_jobs:
        await message.channel.send(
            f"⏳ You already have an active job ({active_jobs[0].id}). "
            "Please wait for it to complete or cancel it first."
        )
        return

    # Create job
    job = job_manager.create_job(stream_url, user_id, message.channel.id, ContentType.LIVE)

    status_msg = await message.channel.send(
        f"🚀 **Starting Live Stream Analysis**\n"
        f"Job ID: `{job.id}`\n"
        f"Stream: {stream_url}\n\n"
        f"📥 Buffering live stream..."
    )

    async def run_pipeline():
        try:
            from vod_intelligence.review_interface import build_review_list_embed, ClipReviewView

            ranking_result = await run_live_pipeline(job.id, job_manager)

            # Load transcript for metadata
            from vod_intelligence.transcript_analyzer import TranscriptAnalyzer
            transcript_analyzer = TranscriptAnalyzer()
            transcript = transcript_analyzer.load_analysis(
                Path(job_manager.get_job(job.id).transcript_path or job_manager.get_job(job.id).analysis_path)
            )

            metadata_list = await generate_metadata_for_all_clips(
                ranking_result.top_clips, transcript, job.streamer_name, job.vod_title, stream_url
            )
            metadata_map = {f"rank_{m.rank}": m for m in metadata_list}

            await status_msg.edit(
                content=(
                    f"✅ **Live Stream Analysis Complete!**\n"
                    f"Job: `{job.id}` | Streamer: **{job.streamer_name}**\n"
                    f"Duration: {job.vod_duration/3600:.1f}h\n\n"
                    f"🎬 **Review Top Clips**"
                )
            )

            if not ranking_result.top_clips:
                await message.channel.send(
                    "⚠️ No reviewable clips were found after ranking. "
                    "Try a different stream or check back later."
                )
                return

            async def on_edit(interaction, job, clip, options):
                await process_vod_clip_edit(interaction, job, clip, options, metadata_map)

            async def on_reject(interaction, clip):
                job.deselect_clip(f"rank_{clip.rank}")
                await interaction.followup.send(f"❌ Rejected clip #{clip.rank}", ephemeral=True)

            async def on_edit_all(interaction, clips):
                for c in clips:
                    job.select_clip(f"rank_{c.rank}")
                await process_vod_clip_edit(interaction, job, clips, {}, metadata_map)

            view = ClipReviewView(
                job=job,
                ranking_result=ranking_result,
                metadata_map=metadata_map,
                job_manager=job_manager,
                on_edit=on_edit,
                on_reject=on_reject,
                on_edit_all=on_edit_all
            )

            embed = build_review_list_embed(job, ranking_result)

            review_msg = await message.channel.send(
                content=(
                    f"🎬 **Edit Options for Job `{job.id}`**\n"
                    f"Use the buttons below to review, edit, or reject clips.\n"
                    f"Top clips: **{len(ranking_result.top_clips)}**"
                ),
                embed=embed,
                view=view
            )

            await message.channel.send(
                "👇 **Select a clip above, then choose an edit option.**\n"
                "• Click a clip number to open its edit menu\n"
                "• Use **Edit All** to process all top clips\n"
                "• Results will be sent to your DM."
            )

        except Exception as e:
            traceback.print_exc()
            await status_msg.edit(
                content=f"❌ **Live Stream Analysis Failed**\nJob: `{job.id}`\nError: {str(e)[:1800]}"
            )

    asyncio.create_task(run_pipeline())


client.run(TOKEN)














