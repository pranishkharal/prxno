import asyncio
import http.server
import re
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import quote, unquote

DOWNLOAD_PORT = 8765
LINK_LIFETIME_SECONDS = 60 * 60

BASE_DIR = Path(__file__).resolve().parent
CLOUDFLARED_PATH = BASE_DIR / "cloudflared.exe"

_lock = threading.Lock()
_files = {}

_server = None
_server_thread = None

_cloudflared = None
_cloudflared_reader_task = None
_public_url = None
_start_lock = asyncio.Lock()


class DownloadHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        try:
            path = unquote(self.path.split("?", 1)[0])

            parts = path.split("/")

            if (
                len(parts) != 4
                or parts[1] != "download"
                or not parts[2]
                or not parts[3]
            ):
                self.send_error(404)
                return

            token = parts[2]
            requested_filename = Path(parts[3]).name

            with _lock:
                entry = _files.get(token)

            if not entry:
                self.send_error(
                    404,
                    "This download link has expired or does not exist."
                )
                return

            if time.time() >= entry["expires"]:
                with _lock:
                    _files.pop(token, None)

                self.send_error(
                    404,
                    "This download link has expired."
                )
                return

            file_path = Path(entry["path"]).resolve()

            if not file_path.exists() or not file_path.is_file():
                self.send_error(404, "File no longer exists.")
                return

            if requested_filename != file_path.name:
                self.send_error(404)
                return

            file_size = file_path.stat().st_size

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "video/mp4"
            )
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{file_path.name}"'
            )
            self.send_header(
                "Content-Length",
                str(file_size)
            )
            self.send_header(
                "Cache-Control",
                "no-store"
            )
            self.end_headers()

            with open(file_path, "rb") as video:
                while True:
                    chunk = video.read(1024 * 1024)

                    if not chunk:
                        break

                    self.wfile.write(chunk)

        except (BrokenPipeError, ConnectionResetError):
            pass

        except Exception as e:
            print("Download server error:", e)


def _start_local_server():
    global _server
    global _server_thread

    with _lock:
        if _server is not None:
            return

        _server = http.server.ThreadingHTTPServer(
            ("127.0.0.1", DOWNLOAD_PORT),
            DownloadHandler
        )

        _server.daemon_threads = True

        _server_thread = threading.Thread(
            target=_server.serve_forever,
            daemon=True
        )

        _server_thread.start()

        print(
            f"Local download server started on "
            f"127.0.0.1:{DOWNLOAD_PORT}"
        )


async def _read_cloudflared_output(process):
    try:
        while True:
            line = await process.stdout.readline()

            if not line:
                break

            text = line.decode(
                errors="ignore"
            ).strip()

            if text:
                print("cloudflared:", text)

    except Exception as e:
        print(
            "cloudflared output reader stopped:",
            e
        )


async def _start_cloudflared():
    global _cloudflared
    global _cloudflared_reader_task
    global _public_url

    if _public_url:
        return _public_url

    async with _start_lock:

        if _public_url:
            return _public_url

        if not CLOUDFLARED_PATH.exists():
            print(
                "ERROR: cloudflared.exe not found:"
                f" {CLOUDFLARED_PATH}"
            )
            return None

        command = [
            str(CLOUDFLARED_PATH),
            "tunnel",
            "--url",
            f"http://127.0.0.1:{DOWNLOAD_PORT}",
            "--no-autoupdate"
        ]

        print(
            "Starting Cloudflare quick tunnel..."
        )

        try:
            _cloudflared = (
                await asyncio.create_subprocess_exec(
                    *command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT
                )
            )

        except Exception as e:
            print(
                "Could not start cloudflared:",
                e
            )
            _cloudflared = None
            return None

        start_time = time.time()

        while time.time() - start_time < 30:

            line = await _cloudflared.stdout.readline()

            if not line:
                break

            text = line.decode(
                errors="ignore"
            ).strip()

            if text:
                print("cloudflared:", text)

            match = re.search(
                r"https://[a-zA-Z0-9-]+\.trycloudflare\.com",
                text
            )

            if match:
                _public_url = match.group(0).rstrip("/")

                print(
                    "Cloudflare tunnel ready:"
                )
                print(_public_url)

                # Continue consuming cloudflared output so
                # the subprocess pipe cannot fill up later.
                _cloudflared_reader_task = asyncio.create_task(
                    _read_cloudflared_output(
                        _cloudflared
                    )
                )

                return _public_url

        print(
            "ERROR: Failed to obtain Cloudflare URL."
        )

        try:
            _cloudflared.terminate()
        except Exception:
            pass

        _cloudflared = None

        return None


async def create_public_download_link(video_path: str) -> str | None:
    video_path = Path(video_path).resolve()

    if not video_path.exists():
        print(
            "Video file not found:",
            video_path
        )
        return None

    if not video_path.is_file():
        print(
            "Not a file:",
            video_path
        )
        return None

    _start_local_server()

    public_url = await _start_cloudflared()

    if not public_url:
        return None

    token = secrets.token_urlsafe(32)

    expires_at = (
        time.time()
        + LINK_LIFETIME_SECONDS
    )

    with _lock:
        _files[token] = {
            "path": str(video_path),
            "expires": expires_at
        }

    filename = quote(
        video_path.name,
        safe=""
    )

    download_url = (
        f"{public_url}/download/"
        f"{token}/{filename}"
    )

    print(
        "========================================"
    )
    print(
        " PUBLIC DOWNLOAD LINK CREATED"
    )
    print(
        "========================================"
    )
    print(
        "File:",
        video_path.name
    )
    print(
        "Expires in: 60 minutes"
    )
    print(
        "URL:",
        download_url
    )

    async def expire_link():
        await asyncio.sleep(
            LINK_LIFETIME_SECONDS
        )

        with _lock:
            removed = _files.pop(
                token,
                None
            )

        if removed:
            print(
                "Download link expired:",
                video_path.name
            )

    asyncio.create_task(
        expire_link()
    )

    return download_url


async def cleanup_expired_links():
    while True:

        await asyncio.sleep(60)

        now = time.time()

        with _lock:
            expired_tokens = [
                token
                for token, entry in _files.items()
                if now >= entry["expires"]
            ]

            for token in expired_tokens:
                _files.pop(
                    token,
                    None
                )

        if expired_tokens:
            print(
                f"Removed {len(expired_tokens)} expired "
                "download link(s)."
            )


def start_cleanup_task():
    try:
        loop = asyncio.get_running_loop()

        loop.create_task(
            cleanup_expired_links()
        )

    except RuntimeError:
        pass
