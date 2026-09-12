with open("main.py", "r", encoding="utf-8") as f:
    content = f.read()

replacements = {
    "from captioning import transcribe_and_caption": "# from captioning import transcribe_and_caption  # temporarily disabled for testing",
    "view=CaptionView(output_file, self.url_streamer_hint)": "# view=CaptionView(output_file, self.url_streamer_hint)  # temporarily disabled for testing",
}

for old, new in replacements.items():
    count = content.count(old)
    content = content.replace(old, new)
    print(f"Replaced {count} occurrence(s) of: {old[:50]}...")

with open("main.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Done.")
