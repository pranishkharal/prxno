import re

with open("main.py", "r", encoding="utf-8") as f:
    content = f.read()

pattern = re.compile(r'emoji="([^"]*)"')

def fix(m):
    s = m.group(1)
    try:
        fixed = s.encode("latin-1").decode("utf-8")
        print(f"Fixed: {s!r} -> {fixed!r}")
        return f'emoji="{fixed}"'
    except Exception as e:
        print(f"Skipped (already OK or unfixable): {s!r} ({e})")
        return m.group(0)

new_content, count = pattern.subn(fix, content)
print(f"\nProcessed {count} emoji field(s) total.")

with open("main.py", "w", encoding="utf-8") as f:
    f.write(new_content)

print("Done.")
