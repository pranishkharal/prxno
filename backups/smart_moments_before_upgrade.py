from pathlib import Path
import json
import re
from faster_whisper import WhisperModel

MODEL_SIZE = "base"

_model = None


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
    return re.sub(
        r"\s+",
        " ",
        str(text or "")
    ).strip()


IMPORTANT_WORDS = {
    "what", "why", "how", "wait",
    "bro", "nah", "no", "nope",
    "crazy", "insane", "actually",
    "seriously", "never", "finally",
    "damn", "wow", "omg", "wtf",
    "look", "listen", "guys", "chat",
    "literally", "impossible",
    "really", "truth"
}


REACTION_WORDS = {
    "laugh", "laughing", "lol",
    "lmao", "haha", "hahaha",
    "scream", "screaming",
    "shout", "shouting",
    "cry", "crying",
    "angry", "mad",
    "crazy", "insane",
    "shock", "shocked",
    "surprised"
}


def word_score(word):
    clean = re.sub(
        r"[^a-zA-Z0-9']",
        "",
        word.lower()
    )

    score = 0

    if clean in IMPORTANT_WORDS:
        score += 2.0

    if clean in REACTION_WORDS:
        score += 3.0

    return score


def analyze_video(video_path):
    video_path = Path(video_path)

    if not video_path.exists():
        raise FileNotFoundError(
            f"Video not found: {video_path}"
        )

    model = get_model()

    segments, info = model.transcribe(
        str(video_path),
        beam_size=5,
        word_timestamps=True,
        vad_filter=True
    )

    all_words = []

    for segment in segments:
        words = getattr(segment, "words", None)

        if not words:
            continue

        for word in words:
            text = normalize(word.word)

            if not text:
                continue

            all_words.append({
                "word": text,
                "start": float(word.start),
                "end": float(word.end),
                "score": word_score(text)
            })

    if not all_words:
        return {
            "duration": 0,
            "word_count": 0,
            "transcript": "",
            "moments": []
        }

    transcript = " ".join(
        word["word"]
        for word in all_words
    )

    duration = max(
        word["end"]
        for word in all_words
    )

    moments = []

    # Analyze overlapping short-form windows.
    window_size = 20.0
    step = 5.0

    current = 0.0

    while current < duration:

        window_start = current
        window_end = min(
            duration,
            current + window_size
        )

        window_words = [
            word
            for word in all_words
            if word["end"] > window_start
            and word["start"] < window_end
        ]

        if window_words:

            word_count = len(window_words)

            important_score = sum(
                word["score"]
                for word in window_words
            )

            speech_density = min(
                1.0,
                word_count / 45.0
            )

            reaction_count = sum(
                1
                for word in window_words
                if word["word"].lower()
                in REACTION_WORDS
            )

            keyword_count = sum(
                1
                for word in window_words
                if word["word"].lower()
                in IMPORTANT_WORDS
            )

            score = (
                speech_density * 35
                + min(30, important_score * 5)
                + min(20, reaction_count * 8)
                + min(15, keyword_count * 5)
            )

            score = min(
                100.0,
                round(score, 2)
            )

            moments.append({
                "start": round(window_start, 2),
                "end": round(window_end, 2),
                "score": score,
                "word_count": word_count,
                "reaction_count": reaction_count,
                "keyword_count": keyword_count,
                "text": " ".join(
                    word["word"]
                    for word in window_words
                )
            })

        current += step

    moments.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    # Keep the strongest non-overlapping moments.
    selected = []

    for moment in moments:

        overlaps = False

        for chosen in selected:

            if (
                moment["start"] < chosen["end"]
                and moment["end"] > chosen["start"]
            ):
                overlaps = True
                break

        if not overlaps:
            selected.append(moment)

        if len(selected) >= 10:
            break

    selected.sort(
        key=lambda item: item["start"]
    )

    return {
        "duration": round(duration, 2),
        "word_count": len(all_words),
        "transcript": transcript,
        "moments": selected
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
    print("========================================")
    print(" SMART MOMENT ANALYSIS")
    print("========================================")
    print(
        f"Duration: {result['duration']} seconds"
    )
    print(
        f"Words: {result['word_count']}"
    )
    print("")

    for index, moment in enumerate(
        result["moments"],
        start=1
    ):
        print(
            f"#{index} "
            f"{moment['start']}s - "
            f"{moment['end']}s "
            f"| SCORE {moment['score']}"
        )

        print(
            f"   {moment['text'][:180]}"
        )

    print("")
