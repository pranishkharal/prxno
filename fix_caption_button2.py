with open("main.py", "r", encoding="utf-8") as f:
    content = f.read()

old_block = """            await self.user.send(
                "\u2705 **Your edited KICK clip is ready!**",
                file=discord.File(
                    str(output_file),
                    filename="edited_kick_clip.mp4"
                )
            )"""

new_block = """            await self.user.send(
                "\u2705 **Your edited KICK clip is ready!**",
                file=discord.File(
                    str(output_file),
                    filename="edited_kick_clip.mp4"
                ),
                view=CaptionView(output_file, self.url_streamer_hint)
            )"""

count = content.count(old_block)
print(f"Found {count} match(es).")

if count == 1:
    content = content.replace(old_block, new_block)
    with open("main.py", "w", encoding="utf-8") as f:
        f.write(content)
    print("Replaced successfully.")
else:
    print("Did not replace - match count was not exactly 1.")
