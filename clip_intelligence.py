from pathlib import Path
import math
import re
import subprocess


IMPORTANT_WORDS = {
    "what",
    "why",
    "how",
    "wait",
    "bro",
    "nah",
    "no",
    "nope",
    "crazy",
    "insane",
    "actually",
    "seriously",
    "never",
    "never",
    "finally",
    "damn",
    "wow",
    "omg",
    "wtf",
    "look",
    "listen",
    "guys",
    "chat",
    "literally",
    "impossible",
    "really",
    "truth",
}


REACTION_WORDS = {
    "laugh",
    "laughing",
    "lol",
    "lmao",
    "haha",
    "hahaha",
    "scream",
    "screaming",
    "shout",
    "shouting",
    "cry",
    "crying",
    "angry",
    "mad",
    "crazy",
    "insane",
    "shock",
    "shocked",
    "surprised",
}


def normalize_text(text):
    return re.sub(
        r"\s+",
        " ",
        str(text or "")
    ).strip()


def score_transcript(text):
    text = normalize_text(text)

    if not text:
        return {
            "score": 0.0,
            "hook_score": 0.0,
            "reaction_score": 0.0,
            "keyword_score": 0.0,
            "word_count": 0,
        }

    words = re.findall(
        r"[A-Za-z0-9']+",
        text.lower()
    )

    if not words:
        return {
            "score": 0.0,
            "hook_score": 0.0,
            "reaction_score": 0.0,
            "keyword_score": 0.0,
            "word_count": 0,
        }

    word_count = len(words)

    important_hits = sum(
        1 for word in words
        if word in IMPORTANT_WORDS
    )

    reaction_hits = sum(
        1 for word in words
        if word in REACTION_WORDS
    )

    keyword_score = min(
        100.0,
        important_hits * 12.0
    )

    reaction_score = min(
        100.0,
        reaction_hits * 18.0
    )

    # Dense speech generally gives us more usable information,
    # while avoiding a huge advantage for extremely long clips.
    density_score = min(
        100.0,
        word_count * 2.5
    )

    first_words = words[:12]

    hook_hits = sum(
        1 for word in first_words
        if word in IMPORTANT_WORDS
    )

    hook_score = min(
        100.0,
        hook_hits * 20.0
    )

    score = (
        hook_score * 0.30
        + reaction_score * 0.25
        + keyword_score * 0.20
        + density_score * 0.25
    )

    return {
        "score": round(score, 2),
        "hook_score": round(hook_score, 2),
        "reaction_score": round(reaction_score, 2),
        "keyword_score": round(keyword_score, 2),
        "word_count": word_count,
    }


def analyze_words(words):
    """
    Accepts faster-whisper word objects and returns
    useful timing statistics.
    """

    cleaned = [
        w for w in (words or [])
        if getattr(w, "word", "").strip()
    ]

    if not cleaned:
        return {
            "speech_seconds": 0.0,
            "silence_seconds": 0.0,
            "speech_density": 0.0,
            "word_count": 0,
        }

    speech_start = float(cleaned[0].start)
    speech_end = float(cleaned[-1].end)

    speech_seconds = max(
        0.0,
        speech_end - speech_start
    )

    word_count = len(cleaned)

    silence_seconds = 0.0

    for previous, current in zip(
        cleaned,
        cleaned[1:]
    ):
        gap = float(current.start) - float(previous.end)

        if gap > 0:
            silence_seconds += gap

    total = speech_seconds + silence_seconds

    density = (
        speech_seconds / total
        if total > 0
        else 0
    )

    return {
        "speech_seconds": round(speech_seconds, 3),
        "silence_seconds": round(silence_seconds, 3),
        "speech_density": round(density, 3),
        "word_count": word_count,
    }


def build_moment_score(
    transcript,
    duration=None,
    speech_density=None
):
    result = score_transcript(transcript)

    score = result["score"]

    if speech_density is not None:
        density_bonus = min(
            15.0,
            max(0.0, float(speech_density)) * 15.0
        )
        score += density_bonus

    if duration is not None:
        duration = float(duration)

        # Favor usable short-form ranges without making
        # duration a hard requirement.
        if 8 <= duration <= 60:
            score += 8
        elif 60 < duration <= 90:
            score += 4

    return {
        **result,
        "score": round(min(100.0, score), 2)
    }
