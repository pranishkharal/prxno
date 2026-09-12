with open("main.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

fixes = {
    2036: '        emoji="\U0001F50D",\n',   # Slightly Zoomed -> magnifying glass
    2128: '        emoji="\u2702\ufe0f",\n',  # Start Edit -> scissors
}

for line_no, new_line in fixes.items():
    idx = line_no - 1
    old = lines[idx]
    print(f"Line {line_no} before: {old.strip()!r}")
    lines[idx] = new_line
    print(f"Line {line_no} after:  {new_line.strip()!r}")

with open("main.py", "w", encoding="utf-8") as f:
    f.writelines(lines)

print("Done.")
