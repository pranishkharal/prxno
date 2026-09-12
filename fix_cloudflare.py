from pathlib import Path
import shutil
from datetime import datetime

p = Path("public_download.py")

backup = p.with_name(
    f"public_download_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.py"
)

shutil.copy2(p, backup)
print(f"Backup created: {backup.name}")

s = p.read_text(encoding="utf-8-sig")

old = """    if _public_url:
        return _public_url

    async with _start_lock:
"""

new = """    # Only reuse the URL if the cloudflared process is still alive.
    if _public_url and _cloudflared is not None:
        try:
            if _cloudflared.returncode is None:
                return _public_url
        except Exception:
            pass

        print("WARNING: Existing Cloudflare tunnel is no longer running.")
        print("Starting a new Cloudflare tunnel...")
        _public_url = None
        _cloudflared = None

    async with _start_lock:
"""

if old not in s:
    print("ERROR: Could not find the expected Cloudflare URL block.")
    raise SystemExit(1)

s = s.replace(old, new, 1)

p.write_text(s, encoding="utf-8")

print("CLOUDFLARE TUNNEL HEALTH FIX INSTALLED")
