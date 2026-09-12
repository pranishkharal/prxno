import os
import re
import subprocess
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "DISCORD_BOT_TOKEN is not set. Put it in .env or set it in PowerShell."
    )

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("output")
OVERLAY_DIR = Path("overlays")

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
OVERLAY_DIR.mkdir(exist_ok=True)

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

# Active editing sessions
SESSIONS = {}


# ---------------------------------------------------------
# FFmpeg helper
# ---------------------------------------------------------

def run_ffmpeg(command):
    print("\nRUNNING FFMPEG:")
    print(" ".join(str(x) for x in command))

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    if result.returncode != 0:
        print("\nFFMPEG ERROR:")
        print(result.stderr[-5000:])
        raise RuntimeError("FFmpeg failed")

    return result


# ---------------------------------------------------------
# Get video information
# ---------------------------------------------------------

def get_video_size(input_file):
    command = [
        "ffprobe",
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        str(input_file)
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    value = result.stdout.strip()

    try:
        width, height = value.split("x")
        return int(width), int(height)
    except Exception:
        return 1920, 1080


# ---------------------------------------------------------
# Remove silence
# ---------------------------------------------------------

def remove_silence(input_file, output_file):

    detect_command = [
        "ffmpeg",
        "-hide_banner",
        "-i",
        str(input_file),
        "-af",
        "silencedetect=noise=-35dB:d=0.7",
        "-f",
        "null",
        "-"
    ]

    result = subprocess.run(
        detect_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    text = result.stderr

    starts = [
        float(x)
        for x in re.findall(r"silence_start:\s*([0-9.]+)", text)
    ]

    ends = [
        float(x)
        for x in re.findall(r"silence_end:\s*([0-9.]+)", text)
    ]

    duration_match = re.search(
        r"Duration:\s*(\d+):(\d+):([\d.]+)",
        text
    )

    if duration_match:
        h = int(duration_match.group(1))
        m = int(duration_match.group(2))
        s = float(duration_match.group(3))
        duration = h * 3600 + m * 60 + s
    else:
        duration = 999999

    # No detected silence
    if not starts and not ends:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(input_file),
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(output_file)
            ],
            check=True
        )
        return

    intervals = []

    current = 0.0

    for start, end in zip(starts, ends):

        if start > current + 0.05:
            intervals.append((current, start))

        current = max(current, end)

    if current < duration - 0.05:
        intervals.append((current, duration))

    if not intervals:
        # Keep original if everything appears silent
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(input_file),
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "aac",
                str(output_file)
            ],
            check=True
        )
        return

    filters = []

    for i, (start, end) in enumerate(intervals):

        filters.append(
            f"[0:v]trim=start={start}:end={end},"
            f"setpts=PTS-STARTPTS[v{i}]"
        )

        filters.append(
            f"[0:a]atrim=start={start}:end={end},"
            f"asetpts=PTS-STARTPTS[a{i}]"
        )

    concat_inputs = ""

    for i in range(len(intervals)):
        concat_inputs += f"[v{i}][a{i}]"

    filters.append(
        concat_inputs
        + f"concat=n={len(intervals)}:v=1:a=1[v][a]"
    )

    filter_complex = ";".join(filters)

    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_file),
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "[a]",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_file)
    ]

    run_ffmpeg(command)



# ---------------------------------------------------------
# AUTOMATIC OVERLAY SYSTEM
# ---------------------------------------------------------

OVERLAY_FILES = {
    "9:16": "kick_916.png",
    "1:1": "kick_11.png",
    "4:5": "kick_45.png",
    "4:3": "kick_43.png",
    "Original": "kick.png",
}

def get_overlay_file(size):
    """
    Automatically select the overlay for the selected aspect ratio.

    Format-specific overlay is preferred.
    If it does not exist, kick.png is used as fallback.
    """

    preferred_name = OVERLAY_FILES.get(size, "kick.png")
    preferred = OVERLAY_DIR / preferred_name

    if preferred.exists():
        print(f"Using automatic overlay: {preferred}")
        return preferred

    fallback = OVERLAY_DIR / "kick.png"

    if fallback.exists():
        print(f"Format overlay not found. Using fallback: {fallback}")
        return fallback

    print("No overlay found in overlays folder.")
    return None


def overlay_position(size):
    """
    Automatic Kick overlay placement.

    9:16:
        Full-screen vertical videos place the watermark
        high enough to stay clear of bottom platform UI.

    1:1:
        Bottom safe area.

    4:5 / 4:3 / Original:
        Bottom safe area.
    """

    if size == "9:16":
        return "H-h-(H*0.25)"

    if size == "1:1":
        return "H-h-(H*0.06)"

    if size == "4:5":
        return "H-h-(H*0.10)"

    if size == "4:3":
        return "H-h-(H*0.08)"

    return "H-h-(H*0.08)"


# ---------------------------------------------------------
# Video editing
# ---------------------------------------------------------

def edit_video(input_file, output_file, options, overlay_file=None):

    size = options["size"]

    # Automatically select the correct overlay.
    # Manual upload is no longer required.
    if options.get("overlay", True) and overlay_file is None:
        overlay_file = get_overlay_file(size)
    zoom = options["zoom"]
    mirror = options["mirror"]
    blur = options["blur"]

    print(
        f"Editing {input_file} with "
        f"{options}"
    )

    # -----------------------------------------------------
    # Target dimensions
    # -----------------------------------------------------

    dimensions = {
        "9:16": (1080, 1920),
        "1:1": (1080, 1080),
        "4:5": (1080, 1350),
        "4:3": (1440, 1080),
        "Original": None
    }

    target = dimensions.get(size)

    # -----------------------------------------------------
    # Build filter
    # -----------------------------------------------------

    filters = []

    if target is None:

        width, height = get_video_size(input_file)

        out_w = width
        out_h = height

        if mirror:
            filters.append("hflip")

        if zoom:
            filters.append(
                "scale=iw*1.08:ih*1.08,"
                "crop=iw/1.08:ih/1.08"
            )

        video_filter = ",".join(filters) if filters else "null"

        if overlay_file:
            command = [
                "ffmpeg",
                "-y",
                "-i",
                str(input_file),
                "-i",
                str(overlay_file),
                "-filter_complex",
                (
                    f"[0:v]{video_filter}[v];"
                    f"[1:v]format=rgba,"
                    f"scale={out_w}:-1[ov];"
                    f"[v][ov]overlay="
                    f"(W-w)/2:H-h-(H*0.25):format=auto[out]"
                ),
                "-map",
                "[out]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
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
                "-i",
                str(input_file),
                "-vf",
                video_filter,
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                str(output_file)
            ]

        run_ffmpeg(command)
        return

    out_w, out_h = target

    # -----------------------------------------------------
    # Background blur mode
    #
    # This creates:
    #
    # blurred full-screen video
    #          +
    # original video centered
    #
    # Similar to vertical social-media editing.
    # -----------------------------------------------------

    if blur:

        foreground_scale = (
            f"scale={out_w}:{out_h}:"
            f"force_original_aspect_ratio=decrease"
        )

        filter_complex = (
            f"[0:v]split=2[bg][fg];"

            f"[bg]"
            f"scale={out_w}:{out_h}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},"
            f"boxblur=25:12,"
            f"setsar=1"
            f"[bgblur];"

            f"[fg]"
            f"{foreground_scale},"
            f"setsar=1"
            f"[fgscaled];"

            f"[bgblur][fgscaled]"
            f"overlay=(W-w)/2:(H-h)/2"
            f"[base]"
        )

    else:

        # Normal crop-to-ratio.
        #
        # IMPORTANT:
        # This avoids the old invalid crop calculation.
        #

        filter_complex = (
            f"[0:v]"
            f"scale={out_w}:{out_h}:"
            f"force_original_aspect_ratio=increase,"
            f"crop={out_w}:{out_h},"
            f"setsar=1"
            f"[base]"
        )

    # -----------------------------------------------------
    # Zoom
    # -----------------------------------------------------

    if zoom:

        filter_complex += (
            ";[base]"
            "scale=iw*1.08:ih*1.08,"
            "crop=iw/1.08:ih/1.08"
            "[zoomed]"
        )

        base_name = "zoomed"

    else:

        base_name = "base"

    # -----------------------------------------------------
    # Mirror
    # -----------------------------------------------------

    if mirror:

        filter_complex += (
            f";[{base_name}]hflip[mirrored]"
        )

        base_name = "mirrored"

    # -----------------------------------------------------
    # Overlay PNG
    # -----------------------------------------------------

    if overlay_file:

        filter_complex += (
            f";[1:v]"
            f"format=rgba,"
            f"scale={out_w}:-1:"
            f"force_original_aspect_ratio=decrease"
            f"[overlay];"

            f"[{base_name}][overlay]"
            f"overlay="
            f"(W-w)/2:"
            f"{overlay_position(size)}:"
            f"format=auto"
            f"[final]"
        )

        map_video = "[final]"

        command = [
            "ffmpeg",
            "-y",
            "-i",
            str(input_file),
            "-i",
            str(overlay_file),
            "-filter_complex",
            filter_complex,
            "-map",
            map_video,
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
            "-i",
            str(input_file),
            "-filter_complex",
            filter_complex,
            "-map",
            f"[{base_name}]",
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
            str(output_file)
        ]

    run_ffmpeg(command)


# ---------------------------------------------------------
# UI
# ---------------------------------------------------------

class EditView(discord.ui.View):

    def __init__(self, user, file_path):

        super().__init__(timeout=900)

        self.user = user
        self.file_path = Path(file_path)

        self.options = {
            "size": "9:16",
            "zoom": False,
            "mirror": False,
            "blur": False,
            "remove_silence": False,
            "overlay": True
        }

        self.overlay_file = None

    async def interaction_check(self, interaction):

        if interaction.user.id != self.user.id:

            await interaction.response.send_message(
                "❌ This edit panel belongs to someone else.",
                ephemeral=True
            )

            return False

        return True

    def summary(self):

        enabled = []

        enabled.append(self.options["size"])

        if self.options["zoom"]:
            enabled.append("Zoom")

        if self.options["mirror"]:
            enabled.append("Mirror")

        if self.options["blur"]:
            enabled.append("Background Blur")

        if self.options["remove_silence"]:
            enabled.append("Remove Silence")

        if self.options["overlay"]:
            enabled.append("Overlay")

        return " • ".join(enabled)

    # -----------------------------------------------------
    # Size dropdown
    # -----------------------------------------------------

    @discord.ui.select(
        placeholder="📐 Choose video size",
        options=[
            discord.SelectOption(label="9:16 Vertical", value="9:16"),
            discord.SelectOption(label="1:1 Square", value="1:1"),
            discord.SelectOption(label="4:5 Portrait", value="4:5"),
            discord.SelectOption(label="4:3", value="4:3"),
            discord.SelectOption(label="Original", value="Original"),
        ]
    )
    async def size_select(self, interaction, select):

        self.options["size"] = select.values[0]

        await interaction.response.edit_message(
            content=(
                "✂️ **AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    # -----------------------------------------------------
    # Zoom
    # -----------------------------------------------------

    @discord.ui.button(
        label="Slightly Zoomed",
        emoji="🔍",
        style=discord.ButtonStyle.secondary
    )
    async def zoom_button(self, interaction, button):

        self.options["zoom"] = not self.options["zoom"]

        button.style = (
            discord.ButtonStyle.success
            if self.options["zoom"]
            else discord.ButtonStyle.secondary
        )

        await interaction.response.edit_message(
            content=(
                "✂️ **AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    # -----------------------------------------------------
    # Mirror
    # -----------------------------------------------------

    @discord.ui.button(
        label="Mirror",
        emoji="🪞",
        style=discord.ButtonStyle.secondary
    )
    async def mirror_button(self, interaction, button):

        self.options["mirror"] = not self.options["mirror"]

        button.style = (
            discord.ButtonStyle.success
            if self.options["mirror"]
            else discord.ButtonStyle.secondary
        )

        await interaction.response.edit_message(
            content=(
                "✂️ **AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    # -----------------------------------------------------
    # Background blur
    # -----------------------------------------------------

    @discord.ui.button(
        label="Background Blur",
        emoji="🌫️",
        style=discord.ButtonStyle.secondary
    )
    async def blur_button(self, interaction, button):

        self.options["blur"] = not self.options["blur"]

        button.style = (
            discord.ButtonStyle.success
            if self.options["blur"]
            else discord.ButtonStyle.secondary
        )

        await interaction.response.edit_message(
            content=(
                "✂️ **AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    # -----------------------------------------------------
    # Remove silence
    # -----------------------------------------------------

    @discord.ui.button(
        label="Remove Silence",
        emoji="🔇",
        style=discord.ButtonStyle.secondary
    )
    async def silence_button(self, interaction, button):

        self.options["remove_silence"] = not self.options["remove_silence"]

        button.style = (
            discord.ButtonStyle.success
            if self.options["remove_silence"]
            else discord.ButtonStyle.secondary
        )

        await interaction.response.edit_message(
            content=(
                "✂️ **AUTO EDIT OPTIONS**\n\n"
                f"Selected: **{self.summary()}**"
            ),
            view=self
        )

    # -----------------------------------------------------
    # Upload overlay
    # -----------------------------------------------------

    @discord.ui.button(
        label="Upload Overlay PNG",
        emoji="🖼️",
        style=discord.ButtonStyle.primary,
        row=2
    )
    async def overlay_button(self, interaction, button):

        SESSIONS[self.user.id] = self

        await interaction.response.send_message(
            "🖼️ **Overlay mode enabled.**\n\n"
            "Now upload your **PNG overlay** in this channel.\n"
            "Make sure it has a transparent background if needed.\n\n"
            "I'll automatically attach it to this edit and place it at the **bottom center**.",
            ephemeral=True
        )

    # -----------------------------------------------------
    # Start edit
    # -----------------------------------------------------

    @discord.ui.button(
        label="Start Edit",
        emoji="✂️",
        style=discord.ButtonStyle.success,
        row=3
    )
    async def start_edit(self, interaction, button):

        await interaction.response.defer()

        output_file = OUTPUT_DIR / (
            f"edited_{self.user.id}_{self.file_path.stem}.mp4"
        )

        try:

            # First remove silence if requested
            working_file = self.file_path

            if self.options["remove_silence"]:

                silence_file = OUTPUT_DIR / (
                    f"silence_removed_{self.user.id}_{self.file_path.stem}.mp4"
                )

                await interaction.followup.send(
                    "🔇 Removing silent sections..."
                )

                remove_silence(
                    working_file,
                    silence_file
                )

                working_file = silence_file

            await interaction.followup.send(
                "🎬 Editing video...\n"
                f"**{self.summary()}**"
            )

            edit_video(
                working_file,
                output_file,
                self.options,
                self.overlay_file
            )

            if not output_file.exists():

                raise RuntimeError(
                    "FFmpeg finished but output file was not created."
                )

            # Send DM
            try:

                await self.user.send(
                    "✅ **Your edited video is ready!**",
                    file=discord.File(
                        str(output_file),
                        filename="edited_video.mp4"
                    )
                )

                await interaction.followup.send(
                    "✅ Done! I sent the edited video to your **DM**."
                )

            except discord.Forbidden:

                await interaction.followup.send(
                    "✅ Video finished, but I couldn't DM you.\n"
                    "Please enable **Allow direct messages from server members**."
                )

                await interaction.followup.send(
                    file=discord.File(
                        str(output_file),
                        filename="edited_video.mp4"
                    )
                )

        except Exception as e:

            print("\nEDIT ERROR:", e)

            await interaction.followup.send(
                f"❌ **EDIT FAILED**\n```{str(e)[:1500]}```"
            )


# ---------------------------------------------------------
# Bot ready
# ---------------------------------------------------------

@client.event
async def on_ready():

    print("----------------------------------")
    print(f"Logged in as {client.user}")
    print("Auto Edit Bot is ready!")
    print("----------------------------------")


# ---------------------------------------------------------
# Messages
# ---------------------------------------------------------

@client.event
async def on_message(message):

    if message.author == client.user:
        return

    # -----------------------------------------------------
    # !edit
    # -----------------------------------------------------

    if not message.content.strip().lower().startswith("!edit"):
        return

    if not message.attachments:

        await message.channel.send(
            "📹 Upload a video with `!edit`."
        )

        return

    attachment = message.attachments[0]

    filename = attachment.filename.lower()

    if not (
        filename.endswith(".mp4")
        or filename.endswith(".mov")
        or filename.endswith(".mkv")
        or filename.endswith(".webm")
        or (
            attachment.content_type
            and attachment.content_type.startswith("video/")
        )
    ):

        await message.channel.send(
            "❌ That isn't a supported video file."
        )

        return

    file_path = UPLOAD_DIR / attachment.filename

    await message.channel.send(
        "📥 Saving your video..."
    )

    await attachment.save(file_path)

    print(f"Video saved: {file_path}")

    view = EditView(
        message.author,
        file_path
    )

    SESSIONS[message.author.id] = view

    await message.channel.send(
        "🎬 **AUTO EDIT OPTIONS**\n\n"
        "Choose everything you want, then press **Start Edit**.\n\n"
        "Selected: **9:16**",
        view=view
    )


# ---------------------------------------------------------
# Start
# ---------------------------------------------------------

client.run(TOKEN)
