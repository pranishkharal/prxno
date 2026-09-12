import os
import re
import difflib
import subprocess
import tempfile
import webrtcvad
from pathlib import Path

import discord
from dotenv import load_dotenv
from rapidocr import RapidOCR

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

# ---------------------------------------------------------
# OCR ENGINE
# ---------------------------------------------------------

# RapidOCR reads the streamer name directly from video frames.
# The bot then matches that name to an overlay filename.
OCR_ENGINE = RapidOCR()

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

# Active editing sessions
SESSIONS = {}


# ---------------------------------------------------------
# FFmpeg helper
# ---------------------------------------------------------

def run_ffmpeg(command, timeout=1800):
    print("\n========================================")
    print("RUNNING FFMPEG")
    print("========================================")
    print(" ".join(str(x) for x in command))
    print("")

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1
    )

    output_lines = []

    try:
        for line in process.stdout:
            line = line.rstrip()

            if line:
                print(line)
                output_lines.append(line)

                # Keep memory usage reasonable on long FFmpeg jobs.
                if len(output_lines) > 1000:
                    output_lines.pop(0)

        return_code = process.wait(timeout=timeout)

    except subprocess.TimeoutExpired:
        process.kill()

        try:
            process.wait(timeout=10)
        except Exception:
            pass

        print("\nFFMPEG TIMEOUT")
        print(f"FFmpeg was allowed to run for {timeout} seconds.")

        raise RuntimeError(
            f"FFmpeg timed out after {timeout} seconds."
        )

    except KeyboardInterrupt:
        process.kill()

        try:
            process.wait(timeout=10)
        except Exception:
            pass

        print("\nFFMPEG INTERRUPTED BY USER")

        raise

    if return_code != 0:
        print("\n========================================")
        print("FFMPEG ERROR")
        print("========================================")

        if output_lines:
            print("\n".join(output_lines[-200:]))

        raise RuntimeError(
            f"FFmpeg failed with exit code {return_code}"
        )

    print("\nFFmpeg finished successfully.")

    return return_code


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
    """
    Remove sections where nobody is speaking.

    Uses WebRTC Voice Activity Detection instead of simply
    looking for quiet audio.

    Behavior:
        - Detects actual speech.
        - Keeps short natural pauses.
        - Removes longer non-speaking sections.
        - Keeps a small buffer around speech so words aren't cut.
    """

    print("")
    print("========================================")
    print(" VOICE ACTIVITY DETECTION")
    print("========================================")
    print("Detecting actual speech...")

    input_file = Path(input_file)
    output_file = Path(output_file)

    # -----------------------------------------------------
    # Extract mono 16 kHz PCM audio.
    # WebRTC VAD works with 10/20/30 ms frames.
    # We use 30 ms.
    # -----------------------------------------------------

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_file),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "-"
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=600
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            "Audio extraction timed out while detecting speech."
        )

    if result.returncode != 0:
        raise RuntimeError(
            "Could not extract audio for speech detection."
        )

    pcm = result.stdout

    # No audio.
    if not pcm:
        print("No audio detected.")
        print("Keeping original video.")

        run_ffmpeg([
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
        ])

        return

    # -----------------------------------------------------
    # WebRTC VAD
    #
    # 0 = least aggressive
    # 3 = most aggressive
    #
    # Mode 2 is a good balance for streamer speech.
    # -----------------------------------------------------

    vad = webrtcvad.Vad(2)

    sample_rate = 16000
    frame_ms = 30
    samples_per_frame = int(
        sample_rate * frame_ms / 1000
    )
    bytes_per_frame = samples_per_frame * 2

    speech_frames = []

    total_frames = len(pcm) // bytes_per_frame

    for index in range(total_frames):

        start_byte = index * bytes_per_frame
        end_byte = start_byte + bytes_per_frame

        frame = pcm[start_byte:end_byte]

        try:
            is_speech = vad.is_speech(
                frame,
                sample_rate
            )
        except Exception:
            is_speech = False

        if is_speech:
            speech_frames.append(index)

    if not speech_frames:
        print("No speech detected.")
        print("Keeping original video.")

        run_ffmpeg([
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
        ])

        return

    # -----------------------------------------------------
    # Convert speech frames into time intervals.
    # -----------------------------------------------------

    raw_intervals = []

    interval_start = speech_frames[0]
    previous = speech_frames[0]

    for frame_index in speech_frames[1:]:

        # If the gap is more than 300 ms, start a new
        # speech region.
        if frame_index - previous > 10:
            start_time = (
                interval_start
                * frame_ms
                / 1000
            )

            end_time = (
                (previous + 1)
                * frame_ms
                / 1000
            )

            raw_intervals.append(
                (start_time, end_time)
            )

            interval_start = frame_index

        previous = frame_index

    raw_intervals.append(
        (
            interval_start * frame_ms / 1000,
            (previous + 1) * frame_ms / 1000
        )
    )

    # -----------------------------------------------------
    # Merge nearby speech regions.
    #
    # This prevents awkward cuts between words/sentences.
    #
    # Any pause <= 0.75 seconds stays.
    # Longer pauses can be removed.
    # -----------------------------------------------------

    merged = []

    for start_time, end_time in raw_intervals:

        if not merged:
            merged.append(
                [start_time, end_time]
            )
            continue

        previous_start, previous_end = merged[-1]

        gap = start_time - previous_end

        if gap <= 0.75:
            merged[-1][1] = end_time
        else:
            merged.append(
                [start_time, end_time]
            )

    # -----------------------------------------------------
    # Add safety padding.
    #
    # Keeps speech from being clipped.
    # -----------------------------------------------------

    padding_before = 0.25
    padding_after = 0.35

    duration = get_video_duration(input_file)

    intervals = []

    for start_time, end_time in merged:

        start_time = max(
            0.0,
            start_time - padding_before
        )

        end_time = min(
            duration,
            end_time + padding_after
        )

        if end_time - start_time >= 0.20:
            intervals.append(
                (start_time, end_time)
            )

    # -----------------------------------------------------
    # Merge again after padding.
    # -----------------------------------------------------

    final_intervals = []

    for start_time, end_time in intervals:

        if not final_intervals:
            final_intervals.append(
                [start_time, end_time]
            )
            continue

        previous_start, previous_end = final_intervals[-1]

        if start_time <= previous_end:
            final_intervals[-1][1] = max(
                previous_end,
                end_time
            )
        else:
            final_intervals.append(
                [start_time, end_time]
            )

    print("")
    print(
        f"Detected {len(final_intervals)} speech sections."
    )

    for index, (start_time, end_time) in enumerate(
        final_intervals,
        start=1
    ):
        print(
            f"Speech {index}: "
            f"{start_time:.2f}s -> {end_time:.2f}s"
        )

    original_duration = duration

    kept_duration = sum(
        end_time - start_time
        for start_time, end_time
        in final_intervals
    )

    removed_duration = max(
        0.0,
        original_duration - kept_duration
    )

    print(
        f"Original duration: {original_duration:.2f}s"
    )

    print(
        f"Speech kept: {kept_duration:.2f}s"
    )

    print(
        f"Non-speaking removed: "
        f"{removed_duration:.2f}s"
    )

    # -----------------------------------------------------
    # If practically nothing was removed, keep a normal
    # transcode instead of creating a complicated concat.
    # -----------------------------------------------------

    if (
        len(final_intervals) == 1
        and final_intervals[0][0] <= 0.01
        and final_intervals[0][1] >= original_duration - 0.05
    ):

        print("No meaningful pauses found.")
        print("Keeping complete video.")

        run_ffmpeg([
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
        ])

        return

    # -----------------------------------------------------
    # Build FFmpeg trim/concat filter.
    # -----------------------------------------------------

    filters = []

    for index, (start_time, end_time) in enumerate(
        final_intervals
    ):

        filters.append(
            f"[0:v]"
            f"trim=start={start_time}:end={end_time},"
            f"setpts=PTS-STARTPTS"
            f"[v{index}]"
        )

        filters.append(
            f"[0:a]"
            f"atrim=start={start_time}:end={end_time},"
            f"asetpts=PTS-STARTPTS"
            f"[a{index}]"
        )

    concat_inputs = ""

    for index in range(len(final_intervals)):
        concat_inputs += (
            f"[v{index}][a{index}]"
        )

    filters.append(
        concat_inputs
        + f"concat=n={len(final_intervals)}:v=1:a=1[v][a]"
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
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_file)
    ]

    run_ffmpeg(command)

    print("")
    print("========================================")
    print(" SPEECH TRIM COMPLETE")
    print("========================================")


# ---------------------------------------------------------
# AUTOMATIC OCR OVERLAY SYSTEM
# ---------------------------------------------------------

OVERLAY_EXTENSIONS = {
    ".webp",
    ".png",
    ".jpg",
    ".jpeg",
}


def normalize_streamer_text(value):
    """
    Convert OCR text and filenames into a comparable form.

    Examples:

        ARAD
        arad
        KICK.COM/ARAD
        Arad Official

    all become comparable to:

        arad
    """

    return re.sub(
        r"[^a-z0-9]+",
        "",
        str(value).lower()
    )


def overlay_base_name(path):
    """
    Convert overlay filenames into streamer names.

    Examples:

        arad.webp
            -> arad

        arad_916.webp
            -> arad

        iamtiagz (1).webp
            -> iamtiagz

        mysteriousaileah (1).webp
            -> mysteriousaileah

        meltt (1).webp
            -> meltt
    """

    stem = path.stem.lower().strip()

    # Remove common Windows duplicate-copy suffixes.
    stem = re.sub(
        r"\s*\(\d+\)\s*$",
        "",
        stem
    )

    # Remove format suffixes.
    stem = re.sub(
        r"_(916|11|45|43)$",
        "",
        stem
    )

    # Normalize the remaining filename.
    return normalize_streamer_text(stem)


def get_overlay_library():
    """
    Build a library of named streamer overlays.

    Filenames are normalized automatically.

    Example:

        iamtiagz (1).webp
        iamtiagz.webp

    both become:

        iamtiagz
    """

    library = {}

    for path in OVERLAY_DIR.iterdir():

        if not path.is_file():
            continue

        if path.suffix.lower() not in OVERLAY_EXTENSIONS:
            continue

        base = overlay_base_name(path)

        # kick.webp is generic and must not become
        # the streamer "kick".
        if not base or base == "kick":
            continue

        library.setdefault(
            base,
            []
        ).append(path)

    return library


def _ocr_texts(image_path):
    """
    Run RapidOCR and return recognized text strings.

    Supports the current RapidOCR result object.
    """

    result = OCR_ENGINE(
        str(image_path)
    )

    texts = getattr(
        result,
        "txts",
        None
    )

    if texts:

        return [
            str(text)
            for text in texts
            if str(text).strip()
        ]

    # Compatibility fallback for older RapidOCR-style results.
    if isinstance(result, tuple) and result:

        rows = result[0]

        if rows:

            output = []

            for row in rows:

                if len(row) > 1:

                    text = str(
                        row[1]
                    )

                    if text.strip():
                        output.append(text)

            return output

    return []


def get_video_duration(input_file):

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(input_file)
    ]

    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    try:

        return float(
            result.stdout.strip()
        )

    except Exception:

        # Safe fallback if ffprobe cannot read duration.
        return 60.0


def detect_streamer_name(input_file):
    """
    Detect the streamer name directly from the video.

    Several frames are sampled because the streamer name may
    be temporarily hidden by animation, movement, or another UI.

    OCR text is compared against the names of overlay files.
    """

    library = get_overlay_library()

    if not library:

        print(
            "OCR: No named overlays found in overlays folder."
        )

        return None

    print("")
    print("========================================")
    print(" OCR STREAMER DETECTION")
    print("========================================")

    print(
        "Available streamer overlays:",
        ", ".join(
            sorted(library.keys())
        )
    )

    duration = max(
        get_video_duration(input_file),
        1.0
    )

    # Multiple points throughout the video.
    sample_ratios = [
        0.05,
        0.20,
        0.40,
        0.60,
        0.80,
        0.95,
    ]

    matches = {
        name: []
        for name in library
    }

    with tempfile.TemporaryDirectory(
        prefix="cfa_ocr_"
    ) as temp_dir:

        temp_dir = Path(
            temp_dir
        )

        for frame_index, ratio in enumerate(
            sample_ratios
        ):

            second = min(
                max(
                    duration * ratio,
                    0.1
                ),
                max(
                    duration - 0.1,
                    0.1
                )
            )

            # Full frame plus a crop focusing on the
            # upper/center portion where streamer names
            # commonly appear.
            variants = [
                (
                    "full",
                    "scale=1600:-2"
                ),
                (
                    "name_area",
                    "crop=iw*0.9:ih*0.65:iw*0.05:0,"
                    "scale=1600:-2"
                ),
            ]

            for variant_index, (
                variant_name,
                video_filter
            ) in enumerate(variants):

                frame = (
                    temp_dir
                    / f"frame_{frame_index}_{variant_index}.jpg"
                )

                command = [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    str(second),
                    "-i",
                    str(input_file),
                    "-an",
                    "-threads",
                    "1",
                    "-frames:v",
                    "1",
                    "-vf",
                    video_filter,
                    "-q:v",
                    "2",
                    "-y",
                    str(frame)
                ]

                try:
                    result = subprocess.run(
                        command,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=30
                    )
                except subprocess.TimeoutExpired:
                    print(
                        f"OCR frame {frame_index + 1} "
                        f"({variant_name}) timed out."
                    )
                    continue

                if (
                    result.returncode != 0
                    or not frame.exists()
                ):
                    continue

                try:

                    texts = _ocr_texts(
                        frame
                    )

                except Exception as ocr_error:

                    print(
                        "OCR frame error:",
                        ocr_error
                    )

                    continue

                if texts:

                    print(
                        f"OCR frame {frame_index + 1} "
                        f"({variant_name}): "
                        f"{texts}"
                    )

                for text in texts:

                    normalized_text = (
                        normalize_streamer_text(
                            text
                        )
                    )

                    if len(
                        normalized_text
                    ) < 3:
                        continue

                    for overlay_name in library:

                        candidate = (
                            normalize_streamer_text(
                                overlay_name
                            )
                        )

                        if len(candidate) < 3:
                            continue

                        # Best case:
                        # ARAD appears inside KICK.COM/ARAD.
                        if candidate in normalized_text:

                            score = 1.0

                        # OCR may capture a shortened version.
                        elif (
                            normalized_text in candidate
                            and len(normalized_text) >= 4
                        ):

                            score = 0.90

                        else:

                            score = (
                                difflib.SequenceMatcher(
                                    None,
                                    candidate,
                                    normalized_text
                                ).ratio()
                            )

                        if score >= 0.72:

                            matches[
                                overlay_name
                            ].append(score)

    ranked = []

    for overlay_name, scores in matches.items():

        if not scores:
            continue

        scores = sorted(
            scores,
            reverse=True
        )

        exact_matches = sum(
            1
            for score in scores
            if score >= 0.99
        )

        strong_matches = sum(
            1
            for score in scores
            if score >= 0.82
        )

        combined_score = (
            scores[0]
            + min(
                sum(scores[1:3]),
                0.50
            )
        )

        # Accept:
        # - an exact match
        # - multiple strong matches
        # - one very strong match
        if (
            exact_matches >= 1
            or strong_matches >= 2
            or scores[0] >= 0.90
        ):

            ranked.append(
                (
                    combined_score,
                    scores[0],
                    overlay_name
                )
            )

    if not ranked:

        print(
            "OCR: Could not confidently identify streamer."
        )

        print(
            "OCR: No streamer overlay will be applied."
        )

        return None

    ranked.sort(
        reverse=True
    )

    best_score, top_score, streamer = ranked[0]

    print(
        f"OCR: Detected streamer = {streamer}"
    )

    print(
        f"OCR: confidence score = {best_score:.2f}"
    )

    print("========================================")
    print("")

    return streamer


def find_overlay_for_streamer(
    streamer_name,
    size
):

    library = get_overlay_library()

    normalized_name = (
        normalize_streamer_text(
            streamer_name
        )
    )

    files = library.get(
        normalized_name,
        []
    )

    if not files:

        print(
            f"No overlay found for streamer: "
            f"{streamer_name}"
        )

        return None

    # Format-specific files are preferred.
    suffix_map = {
        "9:16": "_916",
        "1:1": "_11",
        "4:5": "_45",
        "4:3": "_43",
        "Original": "",
    }

    preferred_suffix = (
        suffix_map.get(
            size,
            ""
        )
    )

    if preferred_suffix:

        preferred = [
            path
            for path in files
            if path.stem.lower().endswith(
                preferred_suffix
            )
        ]

    else:

        preferred = [
            path
            for path in files
            if not re.search(
                r"_(916|11|45|43)$",
                path.stem.lower()
            )
        ]

    pool = (
        preferred
        if preferred
        else files
    )

    # Prefer WEBP over other image formats.
    webp = [
        path
        for path in pool
        if path.suffix.lower() == ".webp"
    ]

    selected = (
        webp[0]
        if webp
        else pool[0]
    )

    print(
        f"Automatic overlay selected: {selected}"
    )

    return selected


def overlay_position(size):
    """
    Safe Kick overlay placement.

    9:16:
        Bottom edge is 25% above the bottom of the video.

    1:1:
        Near the bottom center.

    Other formats:
        Safe lower-center placement.
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


def get_overlay_file(
    size,
    input_file=None
):
    """
    Main automatic overlay selector.

    1. OCR detects streamer.
    2. Streamer name is matched against overlay filename.
    3. Format-specific overlay is preferred.
    4. Generic streamer overlay is used as fallback.

    Example:

        Video says ARAD
             ?
        overlays/arad_916.webp
             ?
        9:16 edit uses arad_916.webp
    """

    if input_file is None:

        print(
            "Overlay detection requires the input video."
        )

        return None

    streamer = detect_streamer_name(
        input_file
    )

    if not streamer:

        # IMPORTANT:
        # Do not accidentally put another streamer's
        # overlay on the video.
        return None

    return find_overlay_for_streamer(
        streamer,
        size
    )



# ---------------------------------------------------------
# Video editing
# ---------------------------------------------------------

def edit_video(input_file, output_file, options, overlay_file=None):

    size = options["size"]

    # Automatically identify the streamer from the video
    # and select the matching overlay.
    if options.get("overlay", True) and overlay_file is None:
        overlay_file = get_overlay_file(
            size,
            input_file
        )
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
                "-loop",
                "1",
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
    # Automatic streamer overlay
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
            "-loop",
            "1",
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
            "remove_silence": True,
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
            enabled.append("Remove Non-Speech")

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
        label="Remove Non-Speech",
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

    named_overlays = get_overlay_library()

    if named_overlays:
        print(
            "Named overlays detected:",
            ", ".join(
                sorted(named_overlays.keys())
            )
        )
    else:
        print(
            "WARNING: No named overlays found in overlays folder."
        )

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
