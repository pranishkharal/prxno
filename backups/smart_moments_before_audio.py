from pathlib import Path
import json
import re
import math
from faster_whisper import WhisperModel

MODEL_SIZE = "base"
_model = None


IMPORTANT_WORDS = {
    "what", "why", "how", "wait", "bro", "nah", "no", "nope",
    "crazy", "insane", "actually", "seriously", "never", "finally",
    "damn", "wow", "omg", "wtf", "look", "listen", "guys", "chat",
    "literally", "impossible", "really", "truth", "wrong", "right"
}

REACTION_WORDS = {
    "laugh", "laughing", "lol", "lmao", "haha", "hahaha",
    "scream", "screaming", "shout", "shouting",
    "cry", "crying", "angry", "mad", "crazy", "insane",
    "shock", "shocked", "surprised", "damn", "wow", "omg", "wtf"
}

QUESTION_WORDS = {
    "what", "why", "how", "where", "when", "who", "which"
}

STRONG_PHRASES = [
    "no way",
    "what the",
    "are you serious",
    "you serious",
    "i can't believe",
    "oh my god",
    "what happened",
    "what are you doing",
    "wait a second",
    "hold on",
    "listen",
    "look at",
]


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


def clean_word(text):
    return re.sub(
        r"[^a-zA-Z0-9']",
        "",
        text.lower()
    )


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
        clean = word["clean"]

        score = 0.0

        if clean in IMPORTANT_WORDS:
            score += 2.0

        if clean in REACTION_WORDS:
            score += 4.0

        if clean in QUESTION_WORDS:
            score += 1.5

        word["importance"] = score


def phrase_score(text):
    text = normalize(text).lower()

    score = 0

    for phrase in STRONG_PHRASES:
        if phrase in text:
            score += 6

    return score


def build_candidates(words):
    if not words:
        return []

    duration = max(
        word["end"]
        for word in words
    )

    candidates = []

    # Multiple window sizes allow short reactions
    # and longer story/context moments.
    window_sizes = [
        8.0,
        12.0,
        16.0,
        20.0
    ]

    for window_size in window_sizes:

        step = 2.0
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

                word_count = len(selected)

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
                    25,
                    density * 25
                )

                importance_score = min(
                    25,
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
                    20,
                    reaction_count * 7
                )

                question_score = min(
                    10,
                    question_count * 3
                )

                phrase_points = min(
                    15,
                    phrase_score(text)
                )

                # Reward changes in speech density.
                before = [
                    word
                    for word in words
                    if start - 5 <= word["start"] < start
                ]

                before_density = len(before) / 5

                current_density = word_count / max(
                    1,
                    end - start
                )

                change = max(
                    0,
                    current_density - before_density
                )

                transition_score = min(
                    10,
                    change * 2
                )

                total = (
                    density_score
                    + importance_score
                    + reaction_score
                    + question_score
                    + phrase_points
                    + transition_score
                )

                total = min(
                    100,
                    round(total, 2)
                )

                candidates.append({
                    "start": round(start, 2),
                    "end": round(end, 2),
                    "score": total,
                    "word_count": word_count,
                    "reaction_count": reaction_count,
                    "question_count": question_count,
                    "speech_density": round(density, 3),
                    "text": text
                })

            start += step

    return candidates


def add_context(moment, duration):
    # Keep some context before and after the
    # strongest detected section.
    context_before = 1.5
    context_after = 1.5

    start = max(
        0,
        moment["start"] - context_before
    )

    end = min(
        duration,
        moment["end"] + context_after
    )

    return {
        "start": round(start, 2),
        "end": round(end, 2)
    }


def select_best(candidates, duration):
    if not candidates:
        return []

    candidates.sort(
        key=lambda item: item["score"],
        reverse=True
    )

    selected = []

    for candidate in candidates:

        overlaps = False

        for existing in selected:

            overlap_start = max(
                candidate["start"],
                existing["start"]
            )

            overlap_end = min(
                candidate["end"],
                existing["end"]
            )

            if overlap_end > overlap_start:
                overlaps = True
                break

        if not overlaps:

            context = add_context(
                candidate,
                duration
            )

            candidate["recommended_start"] = context["start"]
            candidate["recommended_end"] = context["end"]

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

    candidates = build_candidates(words)

    moments = select_best(
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
        print("No strong moments detected.")
    else:

        for index, moment in enumerate(
            result["moments"],
            start=1
        ):
            print(
                f"#{index} "
                f"SCORE {moment['score']}"
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
                f"Questions: {moment['question_count']}"
            )

            print(
                f"Text: {moment['text'][:250]}"
            )

            print("")

