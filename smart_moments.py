from pathlib import Path
import subprocess
import json
import re
import statistics
import struct
from faster_whisper import WhisperModel

MODEL_SIZE = "base"
_model = None

IMPORTANT_WORDS = {
    "what","why","how","wait","bro","nah","no","nope",
    "crazy","insane","actually","seriously","never","finally",
    "damn","wow","omg","wtf","look","listen","guys","chat",
    "literally","impossible","really","truth","wrong","right"
}

REACTION_WORDS = {
    "laugh","laughing","lol","lmao","haha","hahaha",
    "scream","screaming","shout","shouting","cry","crying",
    "angry","mad","crazy","insane","shock","shocked",
    "surprised","damn","wow","omg","wtf"
}

QUESTION_WORDS = {
    "what","why","how","where","when","who","which"
}


def get_model():
    global _model

    if _model is None:
        print("Loading Smart Moment Whisper model...")

        _model = WhisperModel(
            MODEL_SIZE,
            device="cpu",
            compute_type="int8"
        )

        print("Smart Moment Whisper model loaded.")

    return _model


def normalize(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_word(text):
    return re.sub(r"[^a-zA-Z0-9']", "", text.lower())


def transcribe_video(video_path):
    model = get_model()

    segments, info = model.transcribe(
        str(video_path),
        beam_size=5,
        word_timestamps=True,
        vad_filter=True
    )

    words = []

    for segment in segments:
        segment_words = getattr(segment, "words", None)

        if not segment_words:
            continue

        for word in segment_words:
            text = normalize(word.word)

            if not text:
                continue

            words.append({
                "word": text,
                "clean": clean_word(text),
                "start": float(word.start),
                "end": float(word.end)
            })

    return words


def calculate_word_features(words):
    for word in words:
        score = 0.0
        clean = word["clean"]

        if clean in IMPORTANT_WORDS:
            score += 2.0

        if clean in REACTION_WORDS:
            score += 4.0

        if clean in QUESTION_WORDS:
            score += 1.5

        word["importance"] = score


def get_audio_energy(video_path, duration):
    """
    Extract the complete audio as 16-bit PCM and calculate
    RMS energy every 100 ms.
    """

    if duration <= 0:
        return []

    print("Analyzing full audio energy...")

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-i", str(video_path),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-f", "s16le",
        "pipe:1"
    ]

    try:
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

    except Exception as e:
        print("Audio extraction failed:", e)
        return []

    raw = process.stdout

    if not raw:
        print("No PCM audio received.")
        return []

    sample_width = 2
    sample_rate = 16000
    samples_per_window = int(
        sample_rate * 0.1
    )

    bytes_per_window = (
        samples_per_window * sample_width
    )

    rms_values = []

    for offset in range(
        0,
        len(raw),
        bytes_per_window
    ):

        chunk = raw[
            offset:
            offset + bytes_per_window
        ]

        if len(chunk) < 100:
            continue

        count = len(chunk) // 2

        try:
            samples = struct.unpack(
                "<" + ("h" * count),
                chunk[:count * 2]
            )
        except struct.error:
            continue

        if not samples:
            continue

        square_sum = sum(
            sample * sample
            for sample in samples
        )

        rms = (
            square_sum / len(samples)
        ) ** 0.5

        # Convert RMS to dBFS.
        db = 20 * __import__("math").log10(
            max(rms, 1) / 32768
        )

        rms_values.append(db)

    if not rms_values:
        print("Could not calculate RMS.")
        return []

    baseline = statistics.median(
        rms_values
    )

    audio = []

    for index, db in enumerate(rms_values):

        start = index * 0.1
        end = min(
            duration,
            start + 0.1
        )

        difference = db - baseline

        # Stronger-than-normal audio receives
        # a larger score.
        energy_score = max(
            0.0,
            min(
                25.0,
                difference * 3.0
            )
        )

        audio.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "rms_db": round(db, 2),
            "energy_score": round(
                energy_score,
                2
            )
        })

    print(
        f"Audio windows: {len(audio)}"
    )

    print(
        f"Audio baseline: {baseline:.2f} dBFS"
    )

    return audio


def audio_score_for_window(
    audio,
    start,
    end
):
    if not audio:
        return 0.0

    values = [
        item["energy_score"]
        for item in audio
        if item["end"] > start
        and item["start"] < end
    ]

    if not values:
        return 0.0

    return round(
        min(
            25.0,
            statistics.mean(values)
        ),
        2
    )


def build_candidates(
    words,
    audio,
    duration
):
    if not words:
        return []

    candidates = []

    for window_size in (
        6.0,
        8.0,
        10.0,
        12.0,
        16.0
    ):

        start = 0.0

        while start < duration:

            end = min(
                duration,
                start + window_size
            )

            selected = [
                word
                for word in words
                if word["end"] > start
                and word["start"] < end
            ]

            if len(selected) >= 3:

                text = " ".join(
                    word["word"]
                    for word in selected
                )

                speech_seconds = sum(
                    max(
                        0,
                        min(word["end"], end)
                        - max(word["start"], start)
                    )
                    for word in selected
                )

                density = (
                    speech_seconds /
                    max(1, end - start)
                )

                density_score = min(
                    20.0,
                    density * 20
                )

                importance_score = min(
                    20.0,
                    sum(
                        word["importance"]
                        for word in selected
                    )
                )

                reactions = sum(
                    1
                    for word in selected
                    if word["clean"]
                    in REACTION_WORDS
                )

                questions = sum(
                    1
                    for word in selected
                    if word["clean"]
                    in QUESTION_WORDS
                )

                reaction_score = min(
                    15.0,
                    reactions * 5
                )

                question_score = min(
                    10.0,
                    questions * 2.5
                )

                energy_score = (
                    audio_score_for_window(
                        audio,
                        start,
                        end
                    )
                )

                total = min(
                    100.0,
                    round(
                        density_score
                        + importance_score
                        + reaction_score
                        + question_score
                        + energy_score,
                        2
                    )
                )

                candidates.append({
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "score": total,
                    "word_count": len(selected),
                    "reaction_count": reactions,
                    "question_count": questions,
                    "speech_density": round(
                        density,
                        3
                    ),
                    "audio_energy": energy_score,
                    "text": text
                })

            start += 2.0

    candidates.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    return candidates


def select_moments(
    candidates,
    duration
):
    selected = []

    for candidate in candidates:

        overlaps = False

        for existing in selected:

            if (
                candidate["start"]
                < existing["end"]
                and candidate["end"]
                > existing["start"]
            ):
                overlaps = True
                break

        if overlaps:
            continue

        recommended_start = max(
            0.0,
            candidate["start"] - 1.5
        )

        recommended_end = min(
            duration,
            candidate["end"] + 1.5
        )

        candidate["recommended_start"] = round(
            recommended_start,
            2
        )

        candidate["recommended_end"] = round(
            recommended_end,
            2
        )

        selected.append(candidate)

        if len(selected) >= 5:
            break

    return selected


def analyze_video(video_path):
    video_path = Path(video_path)

    if not video_path.exists():
        raise FileNotFoundError(
            f"Video not found: {video_path}"
        )

    words = transcribe_video(video_path)

    if not words:
        return {
            "duration": 0,
            "word_count": 0,
            "transcript": "",
            "moments": []
        }

    calculate_word_features(words)

    duration = max(
        word["end"]
        for word in words
    )

    audio = get_audio_energy(
        video_path,
        duration
    )

    candidates = build_candidates(
        words,
        audio,
        duration
    )

    moments = select_moments(
        candidates,
        duration
    )

    transcript = " ".join(
        word["word"]
        for word in words
    )

    return {
        "duration": round(duration, 2),
        "word_count": len(words),
        "transcript": transcript,
        "moments": moments
    }


def save_analysis(
    video_path,
    output_path=None
):
    result = analyze_video(video_path)

    if output_path is None:
        output_path = Path(
            video_path
        ).with_suffix(
            ".moments.json"
        )

    output_path = Path(output_path)

    output_path.write_text(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    return output_path


if __name__ == "__main__":

    import sys

    if len(sys.argv) < 2:
        print(
            "Usage: python smart_moments.py VIDEO.mp4"
        )
        raise SystemExit(1)

    video = sys.argv[1]

    result = analyze_video(video)

    print("")
    print("==================================================")
    print(" SMART MOMENT ANALYSIS")
    print("==================================================")
    print(
        f"Duration: {result['duration']} seconds"
    )
    print(
        f"Words: {result['word_count']}"
    )
    print("")

    if not result["moments"]:
        print("No moments detected.")
    else:

        for index, moment in enumerate(
            result["moments"],
            start=1
        ):
            print(
                f"#{index} SCORE {moment['score']}"
            )

            print(
                f"Detected: "
                f"{moment['start']}s - "
                f"{moment['end']}s"
            )

            print(
                f"Recommended: "
                f"{moment['recommended_start']}s - "
                f"{moment['recommended_end']}s"
            )

            print(
                f"Words: {moment['word_count']} | "
                f"Reactions: {moment['reaction_count']} | "
                f"Questions: {moment['question_count']} | "
                f"Audio: {moment['audio_energy']}"
            )

            print(
                f"Text: {moment['text'][:250]}"
            )

            print("")
