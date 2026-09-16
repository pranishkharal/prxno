"""VOD downloading: sync yt-dlp helper plus async cached Downloader."""
from __future__ import annotations
from pathlib import Path as _Path
import yt_dlp as _yt_dlp


def _default_cache_root():
    try:
        from vod_agent import settings as _s
        if getattr(_s, "CACHE_ROOT", None):
            return _Path(_s.CACHE_ROOT)
    except Exception:
        pass
    try:
        from infra.config import INFRA_CONFIG
        return _Path(INFRA_CONFIG.cache_root)
    except Exception:
        return _Path("vod_cache")


def _default_temp_dir():
    try:
        from vod_agent import settings as _s
        if getattr(_s, "TEMP_DIR", None):
            return _Path(_s.TEMP_DIR)
    except Exception:
        pass
    try:
        from infra.config import INFRA_CONFIG
        return _Path(INFRA_CONFIG.job_root) / "vod_agent_tmp"
    except Exception:
        return _Path("temp") / "vod_agent_tmp"


def download_vod(url: str, output_dir: str = "vod_cache") -> dict:
    """Sync yt-dlp VOD download (backward compatible)."""
    out = _Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "outtmpl": str(out / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 60,
        "continuedl": True,
        "quiet": False,
        "no_warnings": False,
    }
    print("CHECKPOINT: Starting VOD download")
    print(f"URL: {url}")
    with _yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    f = _Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    if not f.exists():
        found = list(out.glob(f"{info.get('id', '*')}.*"))
        if found:
            f = found[0]
    if not f.exists():
        raise FileNotFoundError(f"Downloaded VOD file was not found in {out}")
    print("CHECKPOINT: VOD download complete")
    print(f"File: {f}")
    return {
        "id": info.get("id"),
        "title": info.get("title"),
        "streamer": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"),
        "filepath": str(f),
    }


import asyncio
import os
import uuid
import aiohttp
import aiofiles


class Downloader:
    def __init__(self, cache_dir=None, temp_dir=None):
        base = _default_cache_root() / "videos" if cache_dir is None else _Path(cache_dir)
        self.CACHE_DIR = str(base)
        os.makedirs(self.CACHE_DIR, exist_ok=True)
        tmp = _default_temp_dir() if temp_dir is None else _Path(temp_dir)
        self.temp_dir = str(tmp / uuid.uuid4().hex)
        os.makedirs(self.temp_dir, exist_ok=True)

    async def download(self, source_url, kick_clip_id):
        """Download video and cache it by kick_clip_id. Thread/process-safe."""
        clip_cache_dir = os.path.join(self.CACHE_DIR, kick_clip_id)
        os.makedirs(clip_cache_dir, exist_ok=True)

        final_file_path = os.path.join(clip_cache_dir, f"{kick_clip_id}.mp4")
        lock_path = final_file_path + ".lock"

        # 1. Already fully downloaded?
        if os.path.exists(final_file_path) and os.path.getsize(final_file_path) > 0:
            return final_file_path

        # 2. Try to acquire lock
        max_wait = 120  # seconds
        waited = 0
        while True:
            try:
                # Exclusive create – only one process can succeed
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break  # we got the lock
            except FileExistsError:
                # Someone else is downloading (or left a stale lock)
                if os.path.exists(final_file_path) and os.path.getsize(final_file_path) > 0:
                    return final_file_path  # they finished while we waited
                if waited >= max_wait:
                    # Stale lock – remove it and try again
                    try:
                        os.remove(lock_path)
                    except OSError:
                        pass
                    waited = 0
                    continue
                await asyncio.sleep(1)
                waited += 1

        # 3. We hold the lock – download
        try:
            # Double-check after acquiring lock
            if os.path.exists(final_file_path) and os.path.getsize(final_file_path) > 0:
                return final_file_path

            temp_file_path = os.path.join(self.temp_dir, f"{uuid.uuid4().hex}.mp4")
            await self._download_to_temp(source_url, temp_file_path)
            if (not os.path.exists(temp_file_path)
                    or os.path.getsize(temp_file_path) == 0):
                raise RuntimeError(f"Download produced an empty file for {kick_clip_id}")
            await asyncio.to_thread(os.replace, temp_file_path, final_file_path)
            return final_file_path
        finally:
            # Always release the lock
            try:
                os.remove(lock_path)
            except OSError:
                pass

    async def _download_to_temp(self, source_url, temp_file_path):
        """Download from source_url and write to temp_file_path."""
        async with aiohttp.ClientSession() as session:
            async with session.get(source_url) as response:
                response.raise_for_status()
                async with aiofiles.open(temp_file_path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(1048576):
                        if chunk:
                            await f.write(chunk)

    async def cleanup(self):
        """Clean up temp directory only."""
        if os.path.exists(self.temp_dir):
            for file in os.listdir(self.temp_dir):
                try:
                    os.remove(os.path.join(self.temp_dir, file))
                except OSError:
                    pass
            try:
                os.rmdir(self.temp_dir)
            except OSError:
                pass


__all__ = ["download_vod", "Downloader"]
