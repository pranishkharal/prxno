with open("main.py", "r", encoding="utf-8") as f:
    content = f.read()

old_block = """    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)"""

new_block = """    try:
        print("CHECKPOINT: about to call yt_dlp.YoutubeDL(...)", flush=True)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            print("CHECKPOINT: calling ydl.extract_info(...) now", flush=True)
            info = ydl.extract_info(url, download=True)
            print("CHECKPOINT: extract_info() returned", flush=True)"""

count = content.count(old_block)
print(f"Found {count} match(es).")

if count == 1:
    content = content.replace(old_block, new_block)
    with open("main.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("Checkpoints added.")
else:
    print("Match failed, no changes made.")
