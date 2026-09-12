with open("main.py", "r", encoding="utf-8") as f:
    content = f.read()

# Insert the semaphore + wrapper function right before run_ffmpeg is defined
marker = "def run_ffmpeg(command, timeout=FFMPEG_TIMEOUT):"

injection = """# Limits how many ffmpeg/edit jobs run at the same time.
# Extra jobs beyond this wait automatically instead of overloading the CPU.
MAX_CONCURRENT_EDITS = 5
EDIT_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_EDITS)

async def run_encode_job(func, *args):
    async with EDIT_SEMAPHORE:
        return await asyncio.to_thread(func, *args)

"""

if marker not in content:
    print("ERROR: marker not found, aborting.")
else:
    content = content.replace(marker, injection + marker, 1)

    old_call = "await asyncio.to_thread("
    new_call = "await run_encode_job("
    count = content.count(old_call)
    content = content.replace(old_call, new_call)
    print(f"Replaced {count} occurrence(s) of asyncio.to_thread with run_encode_job")

    with open("main.py", "w", encoding="utf-8") as f:
        f.write(content)

    print("Done.")
