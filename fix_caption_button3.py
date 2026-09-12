with open("main.py", "r", encoding="utf-8") as f:
    lines = f.readlines()

idx = None
for i, line in enumerate(lines):
    if line.strip() == 'filename="edited_kick_clip.mp4"':
        idx = i
        break

if idx is None:
    print("Target line not found.")
else:
    close_file_idx = idx + 1
    close_send_idx = idx + 2
    print(f"Line {close_file_idx+1}: {lines[close_file_idx]!r}")
    print(f"Line {close_send_idx+1}: {lines[close_send_idx]!r}")

    if lines[close_file_idx].strip() == ")" and lines[close_send_idx].strip() == ")":
        indent = lines[close_file_idx][:len(lines[close_file_idx]) - len(lines[close_file_idx].lstrip())]
        lines[close_file_idx] = indent + "),\n"
        view_line = indent + "view=CaptionView(output_file, self.url_streamer_hint)\n"
        lines.insert(close_file_idx + 1, view_line)

        with open("main.py", "w", encoding="utf-8") as f:
            f.writelines(lines)

        print("Inserted view= successfully.")
    else:
        print("Unexpected structure around this line, aborting.")
