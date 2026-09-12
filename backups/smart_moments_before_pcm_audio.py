from pathlib import Path
import subprocess
import json
import re
import statistics
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
    Uses FFmpeg astats to estimate RMS audio energy
    over short windows.
    """

    if duration <= 0:
        return []

    print("Analyzing audio energy...")

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "info",
        "-i", str(video_path),
        "-af",
        "asetnsamples=n=4800: p=0,astats=metadata=1:reset=1",
        "-f", "null",
        "-"
    ]

    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="ignore"
        )

        output = result.stderr

    except Exception as e:
        print("Audio analysis failed:", e)
        return []

    rms_values = []

    for line in output.splitlines():

        match = re.search(
            r"RMS level dB:\s*(-?\d+(?:\.\d+)?)",
            line
        )

        if match:
            try:
                rms_values.append(
                    float(match.group(1))
                )
            except ValueError:
                pass

    if not rms_values:
        print("No RMS audio data detected.")
        return []

    baseline = statistics.median(rms_values)

    audio = []

    for index, value in enumerate(rms_values):

        # Estimate roughly 0.1 second per astats block.
        timestamp = index * 0.1

        # Convert louder-than-baseline into a useful score.
        difference = value - baseline

        energy_score = max(
            0.0,
            min(
                20.0,
                difference * 2.5
            )
        )

        audio.append({
            "start": round(timestamp, 2),
            "end": round(
                min(duration, timestamp + 0.1),
                2
            ),
            "rms": round(value, 2),
            "energy_score": round(
                energy_score,
                2
            )
        })

    print(
        f"Audio energy samples: {len(audio)}"
    )

    return audio


def audio_score_for_window(audio, start, end):
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

    return min(
        20.0,
        round(statistics.mean(values), 2)
    )


def build_candidates(words, audio, duration):
    if not words:
        return []

    candidates = []

    window_sizes = [
        6.0,
        8.0,
        10.0,
        12.0,
        16.0
    ]

    for window_size in window_sizes:

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

                density = speech_seconds / max(
                    1,
                    end - start
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

                reaction_count = sum(
                    1
                    for word in selected
                    if word["clean"] in REACTION_WORDS
                )

                question_count = sum(
                    1
                    for word in selected
                    if word["clean"] in QUESTION_WORDS
                )

                reaction_score = min(
                    15.0,
                    reaction_count * 5
                )

                question_score = min(
                    10.0,
                    question_count * 2.5
                )

                energy_score = audio_score_for_window(
                    audio,
                    start,
                    end
                )

                total = (
                    density_score
                    + importance_score
                    + reaction_score
                    + question_score
                    + energy_score
                )

                total = min(
                    100.0,
                    round(total, 2)
                )

                candidates.append({
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "score": total,
                    "word_count": len(selected),
                    "reaction_count": reaction_count,
                    "question_count": question_count,
                    "speech_density": round(
                        density,
                        3
                    ),
                    "audio_energy": energy_score,
                    "text": text
                })

            start += 2.0

    candidates.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    return candidates


def add_context(moment, duration):
    start = max(
        0.0,
        moment["start"] - 1.5
    )

    end = min(
        duration,
        moment["end"] + 1.5
    )

    return round(start, 2), round(end, 2)


def select_moments(candidates, duration):
    selected = []

    for candidate in candidates:

        overlap = False

        for existing in selected:

            if (
                candidate["start"] < existing["end"]
                and candidate["end"] > existing["start"]
            ):
                overlap = True
                break

        if overlap:
            continue

        start, end = add_context(
            candidate,
            duration
        )

        candidate["recommended_start"] = start
        candidate["recommended_end"] = end

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


def save_analysis(video_path, output_path=None):
    result = analyze_video(video_path)

    if output_path is None:
        output_path = Path(video_path).with_suffix(
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

