from pathlib import Path
import re

path = Path("main.py")

if not path.exists():
    raise SystemExit("ERROR: main.py was not found.")

text = path.read_text(encoding="utf-8")

# ---------------------------------------------------------
# 1. Add split_photo_file argument to edit_video()
# ---------------------------------------------------------

old = "def edit_video(input_file, output_file, options, overlay_file=None, url_streamer_hint=None):"
new = "def edit_video(input_file, output_file, options, overlay_file=None, url_streamer_hint=None, split_photo_file=None):"

if old not in text:
    raise SystemExit("ERROR: Could not find edit_video() signature.")

text = text.replace(old, new, 1)


# ---------------------------------------------------------
# 2. Insert Split Screen processing into edit_video()
# ---------------------------------------------------------

marker = '''    print(
        f"Editing {input_file} with "
        f"{options}"
    )
'''

if marker not in text:
    raise SystemExit("ERROR: Could not find edit_video() insertion point.")

split_code = r'''
    # ---------------------------------------------------------
    # SPLIT SCREEN
    #
    # 60% VIDEO on top
    # 40% PHOTO on bottom
    # ---------------------------------------------------------
    if options.get("split_screen") and split_photo_file:

        dimensions = {
            "9:16": (1080, 1920),
            "1:1": (1080, 1080),
            "4:5": (1080, 1350),
            "4:3": (1440, 1080),
            "Original": None
        }

        target = dimensions.get(options["size"])

        if target is None:
            src_w, src_h = get_video_size(input_file)
            out_w = src_w
            out_h = src_h
        else:
            out_w, out_h = target

        # Make both sections even-sized for H.264.
        top_h = int(out_h * 0.60)
        top_h = top_h - (top_h % 2)

        bottom_h = out_h - top_h
        bottom_h = bottom_h - (bottom_h % 2)

        # Keep the total output height even.
        out_h = top_h + bottom_h

        zoom = options.get("zoom", False)
        mirror = options.get("mirror", False)

        top_filters = [
            f"scale={out_w}:{top_h}:force_original_aspect_ratio=increase",
            f"crop={out_w}:{top_h}",
            "setsar=1"
        ]

        if zoom:
            top_filters.extend([
                "scale=iw*1.08:ih*1.08",
                "crop=floor(iw/1.08/2)*2:floor(ih/1.08/2)*2"
            ])

        if mirror:
            top_filters.append("hflip")

        enhance_filter = ENHANCE_PRESETS.get(
            options.get("enhance", "Off")
        )

        if enhance_filter:
            top_filters.append(enhance_filter)

        top_filter = ",".join(top_filters)

        # Photo fills the entire bottom 40%.
        photo_filter = (
            f"scale={out_w}:{bottom_h}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={out_w}:{bottom_h},"
            f"setsar=1"
        )

        # Build the filter graph.
        filter_complex = (
            f"[0:v]{top_filter}[top];"
            f"[1:v]{photo_filter}[photo];"
            f"[top][photo]vstack=inputs=2[stack];"
            f"[stack]"
            f"drawbox="
            f"x=0:"
            f"y={top_h - 1}:"
            f"w=iw:"
            f"h=2:"
            f"color=white@0.95:"
            f"t=fill"
            f"[split]"
        )

        final_video = "[split]"

        # Existing streamer overlay remains compatible.
        if overlay_file:
            filter_complex += (
                f";[2:v]"
                f"format=rgba,"
                f"scale={out_w}:-1:"
                f"force_original_aspect_ratio=decrease"
                f"[ov];"
                f"[split][ov]"
                f"overlay="
                f"(W-w)/2:"
                f"{top_h}-h/2:"
                f"format=auto"
                f"[final]"
            )

            final_video = "[final]"

            command = [
                "ffmpeg",
                "-y",

                # Main video
                "-i",
                str(input_file),

                # User photo
                "-loop",
                "1",
                "-i",
                str(split_photo_file),

                # Existing overlay
                "-loop",
                "1",
                "-i",
                str(overlay_file),

                "-filter_complex",
                filter_complex,

                "-map",
                final_video,

                "-map",
                "0:a?",

                "-c:v",
                "libx264",

                "-preset",
                "medium",

                "-crf",
                "20",

                "-pix_fmt",
                "yuv420p",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-shortest",

                str(output_file)
            ]

        else:

            command = [
                "ffmpeg",
                "-y",

                # Main video
                "-i",
                str(input_file),

                # User photo
                "-loop",
                "1",
                "-i",
                str(split_photo_file),

                "-filter_complex",
                filter_complex,

                "-map",
                final_video,

                "-map",
                "0:a?",

                "-c:v",
                "libx264",

                "-preset",
                "medium",

                "-crf",
                "20",

                "-pix_fmt",
                "yuv420p",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-shortest",

                str(output_file)
            ]

        run_ffmpeg(command)
        return
'''

text = text.replace(
    marker,
    marker + split_code,
    1
)


# ---------------------------------------------------------
# 3. Add split_screen option
# ---------------------------------------------------------

old = '''            "overlay": True,
            "enhance": "Off"
'''

new = '''            "overlay": True,
            "enhance": "Off",
            "split_screen": False
'''

if old not in text:
    raise SystemExit("ERROR: Could not find options dictionary.")

text = text.replace(old, new, 1)


# ---------------------------------------------------------
# 4. Add Split Screen to summary()
# ---------------------------------------------------------

old = '''        if self.options.get("enhance", "Off") != "Off":
            enabled.append(f"{self.options['enhance']} Enhance")

        return " â€¢ ".join(enabled)
'''

if old not in text:
    # Try UTF-8 normal bullet version.
    old = '''        if self.options.get("enhance", "Off") != "Off":
            enabled.append(f"{self.options['enhance']} Enhance")

        return " • ".join(enabled)
'''

new = '''        if self.options.get("enhance", "Off") != "Off":
            enabled.append(f"{self.options['enhance']} Enhance")

        if self.options.get("split_screen"):
            enabled.append("Split Screen 60/40")

        return " • ".join(enabled)
'''

if old in text:
    text = text.replace(old, new, 1)
else:
    # More reliable regex fallback.
    pattern = r'(if self\.options\.get\("enhance", "Off"\) != "Off":\s+enabled\.append\(f"\{self\.options\[.enhance.\]\} Enhance"\)\s+)return .*?'
    match = re.search(pattern, text, re.DOTALL)
    if not match:
        raise SystemExit("ERROR: Could not modify summary().")

    replacement = r'''if self.options.get("enhance", "Off") != "Off":
            enabled.append(f"{self.options['enhance']} Enhance")

        if self.options.get("split_screen"):
            enabled.append("Split Screen 60/40")

        return " • ".join(enabled)
'''
    text = text[:match.start()] + replacement + text[match.end():]


# ---------------------------------------------------------
# 5. Add Split Screen button
# ---------------------------------------------------------

marker = '''    @discord.ui.button(
        label="Remove Non-Speech",
        emoji="🔇",
        style=discord.ButtonStyle.secondary
    )
'''

if marker not in text:
    raise SystemExit("ERROR: Could not find Remove Non-Speech button.")

split_button = '''    @discord.ui.button(
        label="Split Screen",
        emoji="🖼️",
        style=discord.ButtonStyle.secondary,
        row=2
    )
    async def split_screen_button(self, interaction, button):

        self.options["split_screen"] = not self.options["split_screen"]

        button.style = (
            discord.ButtonStyle.success
            if self.options["split_screen"]
            else discord.ButtonStyle.secondary
        )

        await interaction.response.edit_message(
            content=(
                "✂️ **AUTO EDIT OPTIONS**\\n\\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

'''

text = text.replace(
    marker,
    split_button + marker,
    1
)


# ---------------------------------------------------------
# 6. Add photo upload request inside Start Edit
# ---------------------------------------------------------

marker = '''        if not self.file_path.exists():

            await notify(
'''

if marker not in text:
    raise SystemExit("ERROR: Could not find Start Edit source-file check.")

# Insert after the existing source-file expiration block.
# We find the line immediately before MAX_UPLOAD_MB.
marker2 = '''        # Discord's practical upload limit for a bot-sent attachment.
'''

if marker2 not in text:
    raise SystemExit("ERROR: Could not find upload-limit marker.")

photo_code = '''        # -------------------------------------------------
        # SPLIT SCREEN PHOTO
        # -------------------------------------------------
        split_photo_file = None

        if self.options.get("split_screen"):

            await notify(
                "🖼️ **Split Screen is enabled.**\\n\\n"
                "Please send the **photo** you want to place "
                "in the bottom 40% of the video.\\n\\n"
                "You have **3 minutes**."
            )

            def split_photo_check(message):
                if message.author.id != self.user.id:
                    return False

                if message.channel.id != self.channel.id:
                    return False

                if not message.attachments:
                    return False

                for attachment in message.attachments:
                    content_type = attachment.content_type or ""
                    filename = attachment.filename.lower()

                    if (
                        content_type.startswith("image/")
                        or filename.endswith((
                            ".jpg",
                            ".jpeg",
                            ".png",
                            ".webp",
                            ".bmp"
                        ))
                    ):
                        return True

                return False

            try:
                photo_message = await self.bot_wait_for_message(
                    split_photo_check,
                    timeout=180
                )
            except asyncio.TimeoutError:
                await notify(
                    "⌛ **Split Screen timed out.**\\n\\n"
                    "No photo was received. Please run `!edit` again."
                )
                return

            image_attachment = None

            for attachment in photo_message.attachments:
                content_type = attachment.content_type or ""
                filename = attachment.filename.lower()

                if (
                    content_type.startswith("image/")
                    or filename.endswith((
                        ".jpg",
                        ".jpeg",
                        ".png",
                        ".webp",
                        ".bmp"
                    ))
                ):
                    image_attachment = attachment
                    break

            if image_attachment is None:
                await notify(
                    "❌ That does not appear to be an image."
                )
                return

            suffix = Path(image_attachment.filename).suffix.lower()

            if suffix not in {
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".bmp"
            }:
                suffix = ".jpg"

            split_photo_file = OUTPUT_DIR / (
                f"split_photo_{self.user.id}_{self.file_path.stem}{suffix}"
            )

            await image_attachment.save(str(split_photo_file))

            if not split_photo_file.exists():
                await notify(
                    "❌ I could not save the Split Screen photo."
                )
                return

            temp_files.append(split_photo_file)

            await notify(
                "✅ **Photo received!**\\n"
                "Video = **60%**\\n"
                "Photo = **40%**\\n"
                "Starting the edit now..."
            )

'''

# The above code uses self.bot_wait_for_message; add a helper to the class
# by replacing it with client.wait_for at runtime.
photo_code = photo_code.replace(
    "self.bot_wait_for_message(",
    "client.wait_for(\"message\", check="
)

# Because client.wait_for already receives the check and timeout,
# fix the generated call.
photo_code = photo_code.replace(
    "client.wait_for(\"message\", check=split_photo_check,\n                    timeout=180\n                )",
    "client.wait_for(\n                    \"message\",\n                    timeout=180,\n                    check=split_photo_check\n                )"
)

text = text.replace(
    marker2,
    photo_code + marker2,
    1
)


# ---------------------------------------------------------
# 7. Pass split_photo_file into edit_video()
# ---------------------------------------------------------

old = '''            self.overlay_file,
            self.url_streamer_hint
        )
'''

new = '''            self.overlay_file,
            self.url_streamer_hint,
            split_photo_file
        )
'''

if old not in text:
    raise SystemExit("ERROR: Could not update edit_video() call.")

text = text.replace(old, new, 1)


# ---------------------------------------------------------
# Save
# ---------------------------------------------------------

path.write_text(text, encoding="utf-8")

print("")
print("========================================")
print(" SPLIT SCREEN PATCH APPLIED")
print("========================================")
print("")
print("Video: 60%")
print("Photo: 40%")
print("Divider: 2px white line")
print("")
print("Backup:")
print("main_backup_before_split.py")
print("")
