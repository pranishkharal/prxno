import sys
import traceback
import yt_dlp

print("PYTHON EXE:", sys.executable)
print("YT-DLP VERSION:", yt_dlp.version.__version__)

url = "https://kick.com/lospollostv/clips/clip_01M1ACG1HV599HTAZ63YMQRXS7"

ydl_opts = {
    "outtmpl": "E:/Auto Clips for Kick/uploads/test_%(id)s.%(ext)s",
    "format": "best[ext=mp4]/best",
    "merge_output_format": "mp4",
    "noplaylist": True,
    "quiet": False,
    "no_warnings": False,
    "retries": 3,
    "fragment_retries": 3,
    "socket_timeout": 60,
    "impersonate": "chrome",
}

print("\n--- STARTING DOWNLOAD ---\n")

try:
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
    print("\n--- SUCCESS ---")
    print("Filename:", info.get("_filename"))
except Exception as e:
    print("\n--- FAILURE ---")
    print("Exception type:", type(e).__name__)
    print("Exception repr:", repr(e))
    print("\nFULL TRACEBACK:")
    traceback.print_exc()
