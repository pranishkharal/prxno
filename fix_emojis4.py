with open("main.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

fixes = {
    2059: '        emoji="\U0001F503",\n',   # Mirror -> 🔃 clockwise arrows
    2082: '        emoji="\u2601\ufe0f",\n',  # Background Blur -> ☁️ cloud
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
