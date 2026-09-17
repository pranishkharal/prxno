import random
import os
import re
import difflib
import subprocess
import tempfile
import asyncio
import json
import time
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

import discord
import aiohttp
from types import SimpleNamespace
from public_download import create_public_download_link, start_cleanup_task
from dotenv import load_dotenv
from rapidocr_onnxruntime import RapidOCR

# yt-dlp is used to download KICK Clips from their public Clip URL.
# Install it with:
#   python -m pip install -U yt-dlp curl_cffi
import yt_dlp


load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

STREAMER_CHANNELS = os.getenv("STREAMER_CHANNELS", "")

def get_streamer_channel():
    """Get the Discord channel ID for a streamer."""
    if not STREAMER_CHANNELS:
        return {}
    mappings = {}
    for item in STREAMER_CHANNELS.split(","):
        parts = item.strip().split(":")
        if len(parts) == 2:
            streamer, channel_id = parts
            if channel_id.isdigit():
                mappings[streamer.lower()] = int(channel_id)
    return mappings

STREAMER_CHANNEL_MAP = get_streamer_channel()


# ---------------------------------------------------------
# Automatic Clip Monitoring
# Polls Kick's clips API and announces new clips to Discord.
# ---------------------------------------------------------

AUTO_CLIP_SEEN_FILE = Path("auto_clip_seen.json")
USER_PROCESS_CHANNELS_FILE = Path("user_process_channels.json")
LEGACY_PROCESS_CHANNELS_FILE = Path("streamer_process_channels.json")
AUTO_CLIP_POLL_SECONDS = max(
    5,
    int(os.getenv("AUTO_CLIP_POLL_SECONDS", "30"))
)
AUTO_CLIP_ANNOUNCE_EXISTING = (
    os.getenv("AUTO_CLIP_ANNOUNCE_EXISTING", "false").lower()
    in {"1", "true", "yes", "on"}
)
AUTO_CLIP_MONITOR_TASK = None

def get_streamer_channel_map():
    """Parse STREAMER_CHANNELS as streamer slug -> Discord channel ID.

    Reads the environment variable every call so that changes take
    effect without a restart.
    """
    mappings = {}

    for item in (os.getenv("STREAMER_CHANNELS", "") or "").split(","):
        streamer, separator, channel_id = item.strip().partition(":")

        if not separator or not streamer or not channel_id.isdigit():
            continue

        mappings[streamer.lower()] = int(channel_id)

    return mappings

def load_seen_auto_clip_ids():
    if not AUTO_CLIP_SEEN_FILE.exists():
        return set()

    try:
        data = json.loads(
            AUTO_CLIP_SEEN_FILE.read_text(encoding="utf-8")
        )
        return set(data if isinstance(data, list) else [])
    except Exception as error:
        print("AUTO CLIPS: could not read seen-clip file:", error)
        return set()

def save_seen_auto_clip_ids(clip_ids):
    AUTO_CLIP_SEEN_FILE.write_text(
        json.dumps(sorted(clip_ids)[-5000:], indent=2),
        encoding="utf-8"
    )

def load_user_process_channels():
    if not USER_PROCESS_CHANNELS_FILE.exists():
        return {}

    try:
        data = json.loads(
            USER_PROCESS_CHANNELS_FILE.read_text(encoding="utf-8")
        )
        return data if isinstance(data, dict) else {}
    except Exception as error:
        print("AUTO CLIPS: could not read user-channel file:", error)
        return {}

def save_user_process_channels(channels):
    USER_PROCESS_CHANNELS_FILE.write_text(
        json.dumps(channels, indent=2),
        encoding="utf-8"
    )

async def remove_legacy_process_channels():
    if not LEGACY_PROCESS_CHANNELS_FILE.exists():
        return

    try:
        legacy_channels = json.loads(
            LEGACY_PROCESS_CHANNELS_FILE.read_text(encoding="utf-8")
        )
    except Exception as error:
        print("AUTO CLIPS: could not read legacy channel file:", error)
        return

    removed = 0
    pending = {}

    for key, channel_id in legacy_channels.items():
        try:
            channel = client.get_channel(int(channel_id))
            if channel is not None:
                await channel.delete(
                    reason="Replace legacy streamer channel with user channel"
                )
                removed += 1
        except discord.NotFound:
            removed += 1
        except Exception as error:
            pending[key] = channel_id
            print(
                f"AUTO CLIPS: could not remove legacy channel {channel_id}: "
                f"{error}"
            )

    if pending:
        LEGACY_PROCESS_CHANNELS_FILE.write_text(
            json.dumps(pending, indent=2),
            encoding="utf-8"
        )
    else:
        LEGACY_PROCESS_CHANNELS_FILE.unlink(missing_ok=True)

    if removed:
        print(f"AUTO CLIPS: removed {removed} legacy streamer channel(s).")


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
MAX_CONCURRENT_EDITS = 5
EDIT_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_EDITS)

async def run_encode_job(func, *args):
    async with EDIT_SEMAPHORE:
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


# ---------------------------------------------------------
# Download progress bar state
# ---------------------------------------------------------

# {user_id: {"percent": float, "status": str}}
PROGRESS_STATE = {}


def render_progress_bar(percent, width=14):
    """Render a [▓▓▓░░░] style progress bar with a percentage."""
    percent = max(0.0, min(100.0, percent))
    filled = int(round(width * percent / 100.0))
    bar = "▓" * filled + "░" * (width - filled)
    return f"[{bar}] {percent:5.1f}%"


def yt_dlp_progress_hook(user_id):
    """Build a yt-dlp progress hook that records download progress."""

    def hook(data):
        state = PROGRESS_STATE.setdefault(user_id, {})
        status = data.get("status")

        if status == "downloading":
            total = data.get("total_bytes") or data.get(
                "total_bytes_estimate"
            )
            downloaded = data.get("downloaded_bytes", 0)
            if total:
                state["percent"] = min(
                    99.9, downloaded / total * 100.0
                )
            state["status"] = "downloading"

        elif status == "finished":
            state["percent"] = 99.9
            state["status"] = "processing"

    return hook


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
        shutil.rmtree(job_dir, ignore_errors=True)

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
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 60,
        "progress_hooks": [yt_dlp_progress_hook(user_id)],
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

    try:
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
        shutil.rmtree(job_dir, ignore_errors=True)

        print("\n========================================")
        print(" KICK DOWNLOAD ERROR (FULL TRACEBACK)")
        print("========================================")
        traceback.print_exc()

        message = str(e).strip() or f"{type(e).__name__} (no message)"

        raise RuntimeError(
            "Could not download the KICK Clip.\n"
            f"{message}\n\n"
            "Make sure the Clip opens normally in your browser and "
            "that yt-dlp/curl_cffi are installed."
        )


def cleanup_job_files(path):
    """
    Delete a downloaded KICK source and its temporary job directory.
    """

    try:
        path = Path(path)

        if path.exists():
            path.unlink()

        # The source lives in uploads/kick_USER_ID/.
        parent = path.parent

        if (
            parent.parent.resolve() == UPLOAD_DIR.resolve()
            and parent.name.startswith("kick_")
        ):
            shutil.rmtree(parent, ignore_errors=True)

    except Exception as e:
        print("Cleanup warning:", e)


# ---------------------------------------------------------
# Automatic Clip Monitoring
# Polls Kick's clips API and announces new clips to Discord.
# ---------------------------------------------------------

async def get_or_create_user_process_channel(announcement_channel, user):
    """Create (or reuse) a private text channel for a user's clip
    processing, so edit interfaces are not visible to the whole
    announcement channel.
    """
    guild = getattr(announcement_channel, "guild", None)
    if guild is None:
        return announcement_channel

    key = f"{guild.id}:{user.id}"
    channels = load_user_process_channels()
    saved_channel_id = channels.get(key)

    bot_member = guild.me
    private_overwrites = {
        role: discord.PermissionOverwrite(view_channel=False)
        for role in guild.roles
    }
    private_overwrites[user] = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True
    )
    if bot_member is not None:
        private_overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
            manage_messages=True
        )

    if saved_channel_id:
        saved_channel = guild.get_channel(int(saved_channel_id))
        if saved_channel is not None:
            await saved_channel.edit(overwrites=private_overwrites)
            return saved_channel

    existing_name = user.name.lower()
    existing_channel = discord.utils.get(
        guild.text_channels,
        name=existing_name
    )

    if existing_channel is not None:
        process_channel = existing_channel
        await process_channel.edit(overwrites=private_overwrites)
    else:
        category = getattr(announcement_channel, "category", None)
        process_channel = await guild.create_text_channel(
            existing_name,
            category=category,
            topic=(
                f"Permanent clip processing channel for {user.name}."
            ),
            overwrites=private_overwrites
        )

        channels[key] = process_channel.id
    save_user_process_channels(channels)
    return process_channel


async def fetch_streamer_clips(session, streamer):
    endpoint = f"https://kick.com/api/v2/channels/{streamer}/clips"

    try:
        async with session.get(endpoint) as response:
            if response.status != 200:
                print(
                    f"AUTO CLIPS: {streamer} returned HTTP {response.status}"
                )
                return []

            payload = await response.json(content_type=None)
            clips = (
                payload.get("clips", [])
                if isinstance(payload, dict) else []
            )
            return clips if isinstance(clips, list) else []

    except Exception as error:
        print(f"AUTO CLIPS: failed to fetch {streamer}: {error}")
        return []


def auto_clip_url(streamer, clip):
    clip_id = str(clip.get("id") or "").strip()
    if not clip_id:
        return None
    return f"https://kick.com/{streamer}/clips/{clip_id}"


async def announce_auto_clip(channel_id, streamer, clip):
    channel = client.get_channel(channel_id)

    if channel is None:
        try:
            channel = await client.fetch_channel(channel_id)
        except Exception as error:
            print(
                f"AUTO CLIPS: could not access Discord channel "
                f"{channel_id} for {streamer}: {error}"
            )
            return False

    clip_url = auto_clip_url(streamer, clip)
    if not clip_url:
        return False

    title = str(clip.get("title") or "Untitled clip").strip()
    creator = (clip.get("creator") or {}).get("username")
    duration = clip.get("duration")
    creator_text = f" by **{creator}**" if creator else ""
    duration_text = f" | {duration}s" if duration else ""

    try:
        await channel.send(
            f"🎬 **New clip from {streamer}**{creator_text}{duration_text}\n"
            f"**{title}**\n{clip_url}",
            view=AutoClipActionsView(clip_url, streamer)
        )
        return True
    except Exception as error:
        print(
            f"AUTO CLIPS: could not post {streamer} clip to "
            f"{channel_id}: {error}"
        )
        return False


async def monitor_auto_clips():
    seen_ids = load_seen_auto_clip_ids()
    first_poll = not AUTO_CLIP_SEEN_FILE.exists()
    mappings = get_streamer_channel_map()

    if not mappings:
        print("AUTO CLIPS: no STREAMER_CHANNELS mappings configured.")
        return

    print(
        f"AUTO CLIPS: monitoring {len(mappings)} streamers every "
        f"{AUTO_CLIP_POLL_SECONDS}s."
    )

    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": "Mozilla/5.0 (KICK clip monitor)"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        while True:
            pass_started = time.monotonic()
            try:
                mappings = get_streamer_channel_map()

                for streamer, channel_id in mappings.items():
                    clips = await fetch_streamer_clips(session, streamer)
                    clips.sort(key=lambda clip: clip.get("created_at") or "")

                    if first_poll and not AUTO_CLIP_ANNOUNCE_EXISTING:
                        seen_ids.update(
                            str(clip.get("id"))
                            for clip in clips
                            if clip.get("id")
                        )
                        continue

                    for clip in clips:
                        clip_id = str(clip.get("id") or "").strip()
                        if not clip_id or clip_id in seen_ids:
                            continue

                        if await announce_auto_clip(channel_id, streamer, clip):
                            seen_ids.add(clip_id)
                            save_seen_auto_clip_ids(seen_ids)

                if first_poll:
                    save_seen_auto_clip_ids(seen_ids)
                    first_poll = False

            except asyncio.CancelledError:
                raise
            except Exception as error:
                print("AUTO CLIPS: monitor loop error:", error)

            # Fixed-rate schedule: every pass starts every
            # AUTO_CLIP_POLL_SECONDS regardless of how long the sequential
            # per-streamer fetches took, so each streamer is checked on a
            # steady interval instead of (interval + pass duration).
            elapsed = time.monotonic() - pass_started
            await asyncio.sleep(max(5.0, AUTO_CLIP_POLL_SECONDS - elapsed))


class AutoClipActionsView(discord.ui.View):
    def __init__(self, clip_url, streamer):
        super().__init__(timeout=3600)
        self.clip_url = clip_url
        self.streamer = streamer

        watch_button = discord.ui.Button(
            label="Watch on KICK",
            style=discord.ButtonStyle.link,
            url=clip_url
        )
        self.add_item(watch_button)

    @discord.ui.button(
        label="Edit Clip",
        emoji="✂️",
        style=discord.ButtonStyle.primary
    )
    async def edit_clip(self, interaction, button):
        await interaction.response.defer(ephemeral=True)

        process_channel = await get_or_create_user_process_channel(
            interaction.channel,
            interaction.user
        )
        message = SimpleNamespace(
            author=interaction.user,
            channel=process_channel
        )
        await start_kick_edit(message, self.clip_url)
        await interaction.followup.send(
            f"Edit options opened in {process_channel.mention}.",
            ephemeral=True
        )

    @discord.ui.button(
        label="Download Clip",
        emoji="⬇️",
        style=discord.ButtonStyle.secondary
    )
    async def download_clip(self, interaction, button):
        await interaction.response.defer(ephemeral=True)

        process_channel = await get_or_create_user_process_channel(
            interaction.channel,
            interaction.user
        )
        message = SimpleNamespace(
            author=interaction.user,
            channel=process_channel
        )
        await handle_download_command(
            message,
            f"!download {self.clip_url}"
        )
        await interaction.followup.send(
            f"Download request started in {process_channel.mention}. "
            "Check your DMs.",
            ephemeral=True
        )


async def handle_download_command(message, command_text):
    """
    Handle a `!download <kick_clip_url>` request.

    Downloads the KICK clip and sends the video file to the
    requester's DMs. Falls back to a temporary Cloudflare download
    link when Discord cannot deliver the file directly.
    """

    user = message.author
    notify = message.channel.send

    # Extract the URL from the command text.
    parts = command_text.split()
    kick_url = parts[1] if len(parts) > 1 else ""

    status = await notify(
        "📥 **Downloading your KICK Clip...**\n"
        "⏳ The video is in under process — please wait."
    )

    try:
        file_path = await run_encode_job(
            download_kick_clip,
            kick_url,
            user.id
        )

        duration = get_video_duration(file_path)

        try:
            await status.edit(
                content=(
                    "✅ **KICK Clip downloaded!**\n"
                    f"Duration: **{duration:.1f}s**\n"
                    "📤 Sending the video to your DMs..."
                )
            )
        except Exception:
            pass

        try:
            await user.send(
                "✅ **Your KICK clip download is ready!**",
                file=discord.File(
                    str(file_path),
                    filename="kick_clip.mp4"
                )
            )

            try:
                await status.edit(
                    content=(
                        "✅ **Done!** I sent the KICK clip "
                        "to your **DMs**."
                    )
                )
            except Exception:
                pass

        except discord.HTTPException as upload_error:

            print("Discord direct upload failed:", upload_error)

            download_url = await create_public_download_link(
                str(file_path)
            )

            if download_url:

                try:
                    await user.send(
                        "✅ **Your KICK clip is ready!**\n\n"
                        "Discord could not send the video directly "
                        "because it is too large.\n\n"
                        "🔗 **Download link:**\n"
                        f"{download_url}\n\n"
                        "⏳ **This link expires in 60 minutes.**"
                    )

                    try:
                        await status.edit(
                            content=(
                                "✅ **Done!** I sent the temporary "
                                "**Cloudflare download link** "
                                "to your DMs."
                            )
                        )
                    except Exception:
                        pass

                except discord.HTTPException:

                    try:
                        await status.edit(
                            content=(
                                "⚠️ I created the download link, "
                                "but I couldn't send you a DM.\n\n"
                                f"🔗 {download_url}"
                            )
                        )
                    except Exception:
                        pass

            else:

                try:
                    await status.edit(
                        content=(
                            "❌ **The download finished, but I couldn't "
                            "deliver the video.**\n\n"
                            "Discord rejected the direct upload and the "
                            "Cloudflare download link could not be created."
                        )
                    )
                except Exception:
                    pass

    except Exception as e:

        print("\nDOWNLOAD COMMAND ERROR:", e)
        traceback.print_exc()

        try:
            await status.edit(
                content=(
                    "❌ **KICK CLIP DOWNLOAD FAILED**\n\n"
                    f"```{str(e)[:1500]}```"
                )
            )
        except Exception:
            pass

        try:
            cleanup_job_files(
                UPLOAD_DIR / f"kick_{user.id}" / "kick_clip.mp4"
            )
        except Exception:
            pass


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

    mirror = options["mirror"]
    blur = options["blur"]
    zoom = options.get("zoom", False)

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
                str(output_file)
            ]

        run_ffmpeg(command)
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
            str(output_file)
        ]

    run_ffmpeg(command)


# ---------------------------------------------------------

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
            "mirror": False,
            "blur": False,
            "overlay": True,
            "enhance": "Off",
            "split_screen": False
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

    def summary(self):

        enabled = []

        enabled.append(self.options["size"])


        if self.options["mirror"]:
            enabled.append("Mirror")

        if self.options["blur"]:
            enabled.append("Background Blur")


        if self.options["overlay"]:
            enabled.append("Overlay")

        if self.options.get("enhance", "Off") != "Off":
            enabled.append(f"{self.options['enhance']} Enhance")

        if self.options.get("split_screen", False):
            enabled.append("Split Screen")

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
                "Ã¢Å“â€šÃ¯Â¸Â **AUTO EDIT OPTIONS**\n\n"
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
                "Ã¢Å“â€šÃ¯Â¸Â **AUTO EDIT OPTIONS**\n\n"
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
                "Ã¢Å“â€šÃ¯Â¸Â **AUTO EDIT OPTIONS**\n\n"
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
                "Ã¢Å“â€šÃ¯Â¸Â **AUTO EDIT OPTIONS**\n\n"
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
            # -------------------------------------------------
            # -------------------------------------------------

            progress_msg = await notify(
                "⏳ **Under process...**\n"
                f"[░░░░░░░░░░░░░░]   0.0%"
            )

            stop_edit_progress = asyncio.Event()

            async def update_edit_progress():
                src_size = max(self.file_path.stat().st_size, 1)
                while not stop_edit_progress.is_set():
                    percent = 0.0
                    try:
                        if output_file.exists():
                            ratio = (
                                output_file.stat().st_size
                                / src_size
                            )
                            percent = min(95.0, ratio * 90.0)
                    except Exception:
                        pass
                    try:
                        await progress_msg.edit(
                            content=(
                                "⏳ **Under process...**\n"
                                f"{render_progress_bar(percent)}"
                            )
                        )
                    except Exception:
                        pass
                    try:
                        await asyncio.wait_for(
                            stop_edit_progress.wait(), timeout=3
                        )
                    except asyncio.TimeoutError:
                        pass

            progress_task = asyncio.create_task(
                update_edit_progress()
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

            stop_edit_progress.set()
            try:
                await progress_task
            except Exception:
                pass

            try:
                await progress_msg.edit(
                    content=(
                        "⏳ **Under process...**\n"
                        f"{render_progress_bar(100.0)}"
                    )
                )
            except Exception:
                pass

            if not output_file.exists():
                raise RuntimeError(
                    "FFmpeg finished but output file was not created."
                )

            final_delivery_file = output_file

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

                await self.user.send(
                    "✅ **Your edited KICK clip is ready!**",
                    file=discord.File(
                        str(final_delivery_file),
                        filename="edited_kick_clip.mp4"
                    )
                )

                await notify(
                    "âœ… Done! I sent the edited KICK clip "
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

    global AUTO_CLIP_MONITOR_TASK

    print("----------------------------------")
    print(f"Logged in as {client.user}")
    print("Auto Edit Bot is ready!")
    print("----------------------------------")

    await remove_legacy_process_channels()

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

    if AUTO_CLIP_MONITOR_TASK is None or AUTO_CLIP_MONITOR_TASK.done():
        AUTO_CLIP_MONITOR_TASK = asyncio.create_task(
            monitor_auto_clips(),
            name="kick-auto-clip-monitor"
        )

    print("----------------------------------")


# ---------------------------------------------------------
# Messages
# ---------------------------------------------------------

@client.event
async def on_message(message):

    if message.author == client.user:
        return

    content = message.content.strip()

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
            "Ã¢ÂÅ’ I found a URL, but it does not look like a KICK Clip URL.\n\n"
            "Use:\n"
            "`!edit https://kick.com/.../clip/...`"
        )

        return

    # If there is no URL after !edit, ask the user for one.
    await message.channel.send(
        "Ã°Å¸Å½Â¬ **KICK AUTO EDIT**\n\n"
        "Paste your **KICK Clip link** here.\n\n"
        "Example:\n"
        "`https://kick.com/.../clip/...`\n\n"
        "You no longer need to upload the video to Discord."
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



async def start_kick_edit(message, kick_url):

    user_id = message.author.id

    if user_id in SESSIONS:

        await message.channel.send(
            "Ã¢ÂÂ³ You already have an edit running. "
            "Please wait for it to finish."
        )

        return

    status = await message.channel.send(
        "🔄 **Step 1/2: Downloading your KICK Clip...**\n"
        "⏳ The video is in under process — please wait.\n"
        "[░░░░░░░░░░░░░░]   0.0%"
    )

    # Live progress bar while downloading.
    stop_progress = asyncio.Event()

    async def update_progress_status():
        while not stop_progress.is_set():
            state = PROGRESS_STATE.get(user_id) or {}
            percent = state.get("percent", 0.0)
            phase = state.get("status", "downloading")
            phrase = (
                "Downloading"
                if phase == "downloading"
                else "Processing downloaded file"
            )
            try:
                await status.edit(
                    content=(
                        "🔄 **Step 1/2: Downloading your KICK Clip...**\n"
                        "⏳ The video is in under process — please wait.\n"
                        f"{render_progress_bar(percent)}  {phrase}"
                    )
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    stop_progress.wait(), timeout=3
                )
            except asyncio.TimeoutError:
                pass

    progress_task = asyncio.create_task(update_progress_status())

    try:

        file_path = await run_encode_job(
            download_kick_clip,
            kick_url,
            user_id
        )

        stop_progress.set()
        try:
            await progress_task
        except Exception:
            pass

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
                "✅ **Step 1/2 complete: KICK Clip downloaded!**\n"
                f"Duration: **{duration:.1f}s**\n"
                f"{creator_line}\n"
                "🎬 **Step 2/2: Editing** — choose your options below, then press "
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
            "Ã°Å¸Å½Â¬ **AUTO EDIT OPTIONS**\n\n"
            "Choose everything you want, then press **Start Edit**.\n\n"
            "Selected: **9:16 Ã¢â‚¬Â¢ Remove Non-Speech Ã¢â‚¬Â¢ Overlay**",
            view=view
        )

    except Exception as e:

        stop_progress.set()
        try:
            await progress_task
        except Exception:
            pass

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

client.run(TOKEN)














