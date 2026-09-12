from pathlib import Path
import re
import shutil
import sys

MAIN = Path("main.py")

if not MAIN.exists():
    print("ERROR: main.py was not found.")
    sys.exit(1)

text = MAIN.read_text(encoding="utf-8")

# ------------------------------------------------------------
# 1. Backup current main.py
# ------------------------------------------------------------

backup = Path("main_before_split_fixed_patch.py")
shutil.copy2(MAIN, backup)
print(f"Backup created: {backup}")

# ------------------------------------------------------------
# 2. Update edit_video() signature
# ------------------------------------------------------------

if "split_photo_file=None" not in text:
    pattern = r"def edit_video\(\s*input_file,\s*output_file,\s*options,\s*overlay_file=None,\s*url_streamer_hint=None\s*\):"

    replacement = """def edit_video(
    input_file,
    output_file,
    options,
    overlay_file=None,
    url_streamer_hint=None,
    split_photo_file=None
):"""

    text, count = re.subn(pattern, replacement, text, count=1)

    if count != 1:
        print("ERROR: Could not update edit_video() signature.")
        sys.exit(1)

    print("OK: edit_video() signature updated.")
else:
    print("OK: edit_video() signature already updated.")

# ------------------------------------------------------------
# 3. Add split_screen option
# ------------------------------------------------------------

if '"split_screen": False' not in text:
    marker = '        "enhance": "Off"\n'

    if marker not in text:
        print("ERROR: Could not find EditView options.")
        sys.exit(1)

    text = text.replace(
        marker,
        '        "enhance": "Off",\n'
        '        "split_screen": False\n',
        1
    )

    print("OK: split_screen option added.")
else:
    print("OK: split_screen option already exists.")

# ------------------------------------------------------------
# 4. Add Split Screen to summary()
# ------------------------------------------------------------

if 'enabled.append("Split Screen 60/40")' not in text:
    marker = '''    if self.options.get("enhance", "Off") != "Off":
        enabled.append(f"{self.options['enhance']} Enhance")

'''

    replacement = '''    if self.options.get("enhance", "Off") != "Off":
        enabled.append(f"{self.options['enhance']} Enhance")

    if self.options.get("split_screen", False):
        enabled.append("Split Screen 60/40")

'''

    if marker not in text:
        print("ERROR: Could not find summary() enhancement section.")
        sys.exit(1)

    text = text.replace(marker, replacement, 1)
    print("OK: Split Screen added to summary.")
else:
    print("OK: Split Screen summary already exists.")

# ------------------------------------------------------------
# 5. Add Split Screen button
# ------------------------------------------------------------

if 'async def split_screen_button' not in text:
    marker = '''@discord.ui.button(
    label="Start Edit",
'''

    button_code = '''@discord.ui.button(
    label="Split Screen",
    emoji="🖼️",
    style=discord.ButtonStyle.secondary,
    row=2
)
async def split_screen_button(self, interaction, button):
    self.options["split_screen"] = not self.options.get("split_screen", False)

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

    if marker not in text:
        print("ERROR: Could not find Start Edit button.")
        sys.exit(1)

    text = text.replace(marker, button_code + marker, 1)
    print("OK: Split Screen button added.")
else:
    print("OK: Split Screen button already exists.")

# ------------------------------------------------------------
# 6. Add photo upload request inside start_edit()
# ------------------------------------------------------------

if 'if self.options.get("split_screen", False):' not in text:
    marker = '''    try:

        # -------------------------------------------------
        # Remove final 4 seconds first.
        # -------------------------------------------------
'''

    photo_code = '''    try:

        # -------------------------------------------------
        # SPLIT SCREEN PHOTO
        # -------------------------------------------------

        split_photo_file = None

        if self.options.get("split_screen", False):

            await notify(
                "🖼️ **Split Screen is enabled.**\\n\\n"
                "Please send the **photo** you want to place "
                "in the bottom 40% of the video.\\n\\n"
                "You have **3 minutes**."
            )

            def split_photo_check(message):
                if message.author.id != self.user.id:
                    return False

                if not self.channel:
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
                        or filename.endswith(".jpg")
                        or filename.endswith(".jpeg")
                        or filename.endswith(".png")
                        or filename.endswith(".webp")
                        or filename.endswith(".bmp")
                    ):
                        return True

                return False

            try:
                photo_message = await client.wait_for(
                    "message",
                    timeout=180,
                    check=split_photo_check
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
                    or filename.endswith(".jpg")
                    or filename.endswith(".jpeg")
                    or filename.endswith(".png")
                    or filename.endswith(".webp")
                    or filename.endswith(".bmp")
                ):
                    image_attachment = attachment
                    break

            if image_attachment is None:
                await notify(
                    "❌ That does not appear to be an image. "
                    "Please run `!edit` again."
                )
                return

            suffix = Path(image_attachment.filename).suffix.lower()

            allowed_extensions = {
                ".jpg",
                ".jpeg",
                ".png",
                ".webp",
                ".bmp"
            }

            if suffix not in allowed_extensions:
                suffix = ".jpg"

            split_photo_file = OUTPUT_DIR / (
                f"split_photo_{self.user.id}_"
                f"{self.file_path.stem}{suffix}"
            )

            try:
                await image_attachment.save(str(split_photo_file))
            except Exception as photo_error:
                await notify(
                    "❌ Could not save the Split Screen photo.\\n\\n"
                    f"`{str(photo_error)[:1000]}`"
                )
                return

            if not split_photo_file.exists():
                await notify(
                    "❌ The Split Screen photo was not saved correctly."
                )
                return

            temp_files.append(split_photo_file)

            await notify(
                "✅ **Photo received!**\\n\\n"
                "Video = **60%**\\n"
                "Photo = **40%**\\n"
                "Divider = **2px**\\n\\n"
                "Starting the edit now..."
            )

        # -------------------------------------------------
        # Remove final 4 seconds first.
        # -------------------------------------------------
'''

    if marker not in text:
        print("ERROR: Could not find start_edit() try block.")
        sys.exit(1)

    text = text.replace(marker, photo_code, 1)
    print("OK: Split Screen photo upload added.")
else:
    print("OK: Split Screen photo upload already exists.")

# ------------------------------------------------------------
# 7. Pass split_photo_file into run_encode_job(edit_video...)
# ------------------------------------------------------------

if "self.url_streamer_hint,\n            split_photo_file" not in text:
    pattern = r'''(await run_encode_job\(\s*
            edit_video,\s*
            working_file,\s*
            output_file,\s*
            self\.options,\s*
            self\.overlay_file,\s*
            self\.url_streamer_hint\s*
        \))'''

    replacement = '''await run_encode_job(
            edit_video,
            working_file,
            output_file,
            self.options,
            self.overlay_file,
            self.url_streamer_hint,
            split_photo_file
        )'''

    text, count = re.subn(pattern, replacement, text, count=1)

    if count != 1:
        print("ERROR: Could not update edit_video() call.")
        print("")
        print("The existing call was not in the expected format.")
        print("No file was written.")
        sys.exit(1)

    print("OK: split_photo_file passed into edit_video().")
else:
    print("OK: split_photo_file already passed into edit_video().")

# ------------------------------------------------------------
# 8. Add the actual 60/40 FFmpeg Split Screen engine
# ------------------------------------------------------------

if 'SPLIT SCREEN 60/40 ENGINE' not in text:

    marker = '''    dimensions = {
        "9:16": (1080, 1920),
        "1:1": (1080, 1080),
        "4:5": (1080, 1350),
        "4:3": (1440, 1080),
        "Original": None
    }

    target = dimensions.get(size)

'''

    split_engine = '''    # ---------------------------------------------------------
    # SPLIT SCREEN 60/40 ENGINE
    # ---------------------------------------------------------
    #
    # Top 60%  = video
    # Bottom 40% = uploaded photo
    # Divider = 2px white line
    #
    # Zoom / Mirror / Enhance affect the video section.
    # Existing Overlay is preserved and placed around the
    # split boundary.
    # ---------------------------------------------------------

    if options.get("split_screen", False):

        if split_photo_file is None:
            raise RuntimeError(
                "Split Screen is enabled but no photo was supplied."
            )

        if not Path(split_photo_file).exists():
            raise RuntimeError(
                "Split Screen photo file does not exist."
            )

        if size == "Original":
            out_w, out_h = get_video_size(input_file)
        else:
            out_w, out_h = dimensions.get(
                size,
                (1080, 1920)
            )

        # FFmpeg works best with even dimensions.
        out_w = max(2, int(out_w))
        out_h = max(2, int(out_h))

        if out_w % 2:
            out_w -= 1

        if out_h % 2:
            out_h -= 1

        # Exact 60 / 40 split.
        top_h = int(out_h * 0.60)
        top_h -= top_h % 2

        bottom_h = out_h - top_h
        bottom_h -= bottom_h % 2

        # Keep total height exactly equal to output height.
        bottom_h = out_h - top_h

        if bottom_h % 2:
            bottom_h -= 1
            out_h = top_h + bottom_h

        print("")
        print("========================================")
        print(" SPLIT SCREEN 60/40")
        print("========================================")
        print(f"Canvas: {out_w}x{out_h}")
        print(f"Video section: {out_w}x{top_h}")
        print(f"Photo section: {out_w}x{bottom_h}")
        print("Divider: 2px")
        print("")

        # -----------------------------
        # VIDEO SECTION
        # -----------------------------

        video_filters = [
            f"scale={out_w}:{top_h}:"
            "force_original_aspect_ratio=increase",
            f"crop={out_w}:{top_h}"
        ]

        if zoom:
            video_filters.extend([
                "scale=iw*1.08:ih*1.08",
                "crop=floor(iw/1.08/2)*2:"
                "floor(ih/1.08/2)*2"
            ])

        if mirror:
            video_filters.append("hflip")

        enhance_filter = ENHANCE_PRESETS.get(
            options.get("enhance", "Off")
        )

        if enhance_filter:
            video_filters.append(enhance_filter)

        video_filters.append("setsar=1")

        video_filter = ",".join(video_filters)

        # -----------------------------
        # PHOTO SECTION
        # -----------------------------

        photo_filter = (
            f"[1:v]"
            f"scale={out_w}:{bottom_h}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={out_w}:{bottom_h},"
            f"setsar=1"
            f"[photo]"
        )

        # -----------------------------
        # STACK VIDEO + PHOTO
        # -----------------------------

        filter_parts = [
            f"[0:v]{video_filter}[video]",
            photo_filter,
            "[video][photo]vstack=inputs=2[stack]"
        ]

        # -----------------------------
        # EXISTING OVERLAY
        # -----------------------------

        if overlay_file:

            filter_parts.append(
                f"[2:v]"
                f"format=rgba,"
                f"scale={out_w}:-1:"
                f"force_original_aspect_ratio=decrease"
                f"[ov]"
            )

            filter_parts.append(
                f"[stack][ov]"
                f"overlay=(W-w)/2:"
                f"{top_h}-h/2:"
                f"format=auto"
                f"[withoverlay]"
            )

            # Draw divider AFTER overlay so the divider
            # always remains visible.
            filter_parts.append(
                f"[withoverlay]"
                f"drawbox="
                f"x=0:"
                f"y={top_h - 1}:"
                f"w={out_w}:"
                f"h=2:"
                f"color=white:"
                f"t=fill"
                f"[final]"
            )

        else:

            filter_parts.append(
                f"[stack]"
                f"drawbox="
                f"x=0:"
                f"y={top_h - 1}:"
                f"w={out_w}:"
                f"h=2:"
                f"color=white:"
                f"t=fill"
                f"[final]"
            )

        filter_complex = ";".join(filter_parts)

        command = [
            "ffmpeg",
            "-y",

            # Video
            "-i",
            str(input_file),

            # Uploaded photo
            "-loop",
            "1",
            "-framerate",
            "30",
            "-i",
            str(split_photo_file)
        ]

        # Existing overlay becomes input #2.
        if overlay_file:
            command.extend([
                "-loop",
                "1",
                "-i",
                str(overlay_file)
            ])

        command.extend([
            "-filter_complex",
            filter_complex,

            "-map",
            "[final]",

            # Keep original video audio.
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

            "-movflags",
            "+faststart",

            str(output_file)
        ])

        run_ffmpeg(command)
        return

'''

    if marker not in text:
        print("ERROR: Could not find dimensions section in edit_video().")
        sys.exit(1)

    text = text.replace(marker, marker + split_engine, 1)
    print("OK: FFmpeg Split Screen engine added.")
else:
    print("OK: Split Screen FFmpeg engine already exists.")

# ------------------------------------------------------------
# 9. Write file
# ------------------------------------------------------------

MAIN.write_text(text, encoding="utf-8")

print("")
print("========================================")
print(" SPLIT SCREEN PATCH COMPLETE")
print("========================================")
print("")
print("Video: 60%")
print("Photo: 40%")
print("Divider: 2px")
print("")
print("Backup:")
print(backup)
print("")

