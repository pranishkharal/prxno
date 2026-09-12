from pathlib import Path
import subprocess
import tempfile
from faster_whisper import WhisperModel

MODEL_SIZE = "base"
_model = None


def get_model():
    global _model

    if _model is None:
        print("Loading Whisper model...")
        _model = WhisperModel(
            MODEL_SIZE,
            device="cpu",
            compute_type="int8"
        )
        print("Whisper model loaded.")

    return _model


def ass_time(seconds):
    seconds = max(0, float(seconds))

    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centiseconds = int((seconds - int(seconds)) * 100)

    return f"{hours}:{minutes:02d}:{secs:02d}.{centiseconds:02d}"


def escape_ass(text):
    return (
        text
        .replace("\\", r"\\")
        .replace("{", r"\{")
        .replace("}", r"\}")
    )


def create_word_by_word_ass(video_path, ass_path):
    model = get_model()

    segments, info = model.transcribe(
        str(video_path),
        beam_size=5,
        word_timestamps=True,
        vad_filter=True
    )

    lines = []

    for segment in segments:
        words = getattr(segment, "words", None)

        if not words:
            continue

        words = [
            word for word in words
            if word.word.strip()
        ]

        for i, word in enumerate(words):
            current = word.word.strip()
            start = word.start

            if i + 1 < len(words):
                end = words[i + 1].start
            else:
                end = word.end

            if end <= start:
                end = start + 0.25

            before = [
                w.word.strip()
                for w in words[max(0, i - 2):i]
                if w.word.strip()
            ]

            after = [
                w.word.strip()
                for w in words[i + 1:i + 3]
                if w.word.strip()
            ]

            parts = []

            for w in before:
                parts.append(
                    r"{\c&HFFFFFF&}" + escape_ass(w)
                )

            parts.append(
                r"{\c&H00FFFF&\b1}" +
                escape_ass(current) +
                r"{\b0}"
            )

            for w in after:
                parts.append(
                    r"{\c&HFFFFFF&}" + escape_ass(w)
                )

            text = " ".join(parts)

            lines.append(
                f"Dialogue: 0,"
                f"{ass_time(start)},"
                f"{ass_time(end)},"
                f"WordCaption,,,,0,0,0,,"
                f"{text}"
            )

    ass_content = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: WordCaption,Arial,72,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,80,80,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    ass_content += "\n".join(lines)

    Path(ass_path).write_text(
        ass_content,
        encoding="utf-8"
    )

    return len(lines)


def burn_word_captions(input_video, output_video):
    input_video = Path(input_video).resolve()
    output_video = Path(output_video).resolve()

    with tempfile.TemporaryDirectory() as temp_dir:
        ass_file = Path(temp_dir) / "captions.ass"

        count = create_word_by_word_ass(
            input_video,
            ass_file
        )

        if count == 0:
            raise RuntimeError(
                "No speech/words were detected in the video."
            )

        print(f"Generated {count} caption events.")

        # Convert Windows path to FFmpeg-compatible filter path.
        ass_filter_path = ass_file.as_posix()
        ass_filter_path = ass_filter_path.replace(":", r"\:")

        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(input_video),
                "-vf",
                f"ass='{ass_filter_path}'",
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
                "-movflags",
                "+faststart",
                str(output_video)
            ],
            check=True
        )

    return output_video
