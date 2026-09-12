import re

with open("main.py", "rb") as f:
    raw = f.read()

text = raw.decode("utf-8", errors="replace")
lines = text.splitlines()

for i, line in enumerate(lines, start=1):
    if "emoji=" in line:
        print(f"Line {i}: {line.strip()!r}")
