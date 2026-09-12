"""
Local intelligent caption generation for the Auto Edit bot.

Pipeline:
1. Whisper transcribes the actual video.
2. The transcript is analyzed by Ollama.
3. caption_guide.json provides the full viral-caption strategy.
4. Ollama chooses the strategy that best matches THIS clip.
5. A ready-to-post caption is returned.
"""

import json
import random
from pathlib import Path

import requests
import whisper


_WHISPER_MODEL = None

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2"

CAPTION_GUIDE_PATH = Path(__file__).resolve().parent / "caption_guide.json"


def get_whisper_model():
    """
    Lazily load Whisper once and reuse it.
    """
    global _WHISPER_MODEL

    if _WHISPER_MODEL is None:
        print(
            "Loading local Whisper model "
            "(first run may take a moment)...",
            flush=True
        )

        _WHISPER_MODEL = whisper.load_model("base")

    return _WHISPER_MODEL


def transcribe_video(video_path):
    """
    Transcribe the actual audio from the video.
    """
    model = get_whisper_model()

    result = model.transcribe(
        str(video_path)
    )

    transcript = result.get(
        "text",
        ""
    ).strip()

    print(
        f"Transcript length: {len(transcript)} characters",
        flush=True
    )

    return transcript


def load_caption_guide():
    """
    Load the full caption strategy from caption_guide.json.
    """
    try:
        with open(
            CAPTION_GUIDE_PATH,
            "r",
            encoding="utf-8-sig"
        ) as f:
            guide = json.load(f)

        print(
            f"Caption guide loaded: "
            f"{len(json.dumps(guide, ensure_ascii=False))} characters",
            flush=True
        )

        return json.dumps(
            guide,
            ensure_ascii=False,
            indent=2
        )

    except Exception as error:
        print(
            "WARNING: Could not load caption_guide.json:",
            error,
            flush=True
        )

        return ""


CAPTION_STYLE_GUIDE = load_caption_guide()


def _ollama_available():
    """
    Check whether local Ollama is running.
    """
    try:
        response = requests.get(
            "http://localhost:11434",
            timeout=1.5
        )

        return response.status_code < 500

    except Exception:
        return False


def _clean_ollama_caption(text):
    """
    Extract only the final ready-to-post caption from Ollama output.
    Handles cases where the model includes analysis/reasoning.
    """
    if not text:
        return ""

    text = text.strip()

    # Remove common markdown/code formatting.
    text = text.replace("```text", "").replace("```", "").strip()

    # If the model explicitly gives a final-caption section,
    # keep only what comes after it.
    markers = [
        "Here's the final caption:",
        "Here is the final caption:",
        "Final caption:",
        "FINAL CAPTION:",
        "Output:",
        "OUTPUT:",
    ]

    for marker in markers:
        if marker in text:
            text = text.split(marker, 1)[1].strip()
            break

    # Remove leading/trailing quotation marks.
    text = text.strip().strip('"').strip("'").strip()

    # If multiple lines remain, find the most caption-like line.
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    if not lines:
        return ""

    # Remove obvious explanatory lines.
    bad_starts = (
        "let's analyze",
        "here's",
        "here is",
        "the streamer",
        "based on",
        "i would choose",
        "i choose",
        "the clip",
        "* ",
        "- ",
    )

    candidates = []

    for line in lines:
        clean = line.strip().strip('"').strip("'").strip()

        if not clean:
            continue

        lower = clean.lower()

        if lower.startswith(bad_starts):
            continue

        if clean.startswith("Caption:"):
            clean = clean[len("Caption:"):].strip()

        if clean:
            candidates.append(clean)

    if candidates:
        # Prefer the last candidate because Ollama usually puts
        # the final caption at the end.
        text = candidates[-1]

    # Final cleanup.
    text = text.strip().strip('"').strip("'").strip()

    return text


def generate_caption_with_ollama(
    transcript,
    streamer_name
):
    """
    Use the transcript + full caption guide
    to generate a specific caption for this clip.
    """

    guide = CAPTION_STYLE_GUIDE

    prompt = f"""You are an expert viral streamer-clip caption writer.

Your job is to produce ONE ready-to-post caption for THIS specific clip.

ACTUAL VIDEO TRANSCRIPT:
{transcript}

STREAMER: {streamer_name or "the streamer"}

CAPTION GUIDE:
{guide}

RULES:
1. Base the caption on the ACTUAL transcript.
2. Use specific details from the transcript (names, emotions, actions).
3. Do NOT invent events, names, money, or consequences.
4. If the transcript mentions "scared", "Francisco", "Frankie", or "stop", use those words in the caption.
5. Return ONLY the final caption. No explanations, no analysis.
6. Keep it suitable for TikTok / Instagram Reels / YouTube Shorts.
7. Use 1-2 emojis max.

FINAL CAPTION:"""

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.7,
                "num_predict": 120
            }
        },
        timeout=120
    )

    response.raise_for_status()

    data = response.json()

    caption = data.get(
        "response",
        ""
    )

    caption = _clean_ollama_caption(caption)

    return caption or None


CAPTION_TEMPLATES = {
    "apology": [
        "the wholesome moment {streamer} showed MATURITY and apologized after what happened ❤️‍🩹"
    ],

    "emotional": [
        "the emotional moment {streamer} realized what happened ❤️‍🩹",
        "{streamer} was HEARTBROKEN after what happened ❤️‍🩹"
    ],

    "career": [
        "{streamer} had his \"I MADE IT MOMENT\" and was emotional after what happened ❤️‍🩹"
    ],

    "celebrity": [
        "the moment {streamer} had NO IDEA who he just met ❤️‍🩹"
    ],

    "funny": [
        "{streamer} did NOT expect that to happen 😭"
    ],

    "scared": [
        "{streamer} got SCARED and said \"I'm scared, bro\" 😱",
        "{streamer} was terrified when this happened 😱"
    ],

    "francisco": [
        "the moment {streamer} realized what Francisco was doing 😱",
        "{streamer} was confused when Francisco showed up 😱"
    ],

    "stop": [
        "{streamer} said STOP and you won't believe what happened next 😱",
        "{streamer} called for a STOP and everything changed 😱"
    ],

    "default": [
        "{streamer} REACTS live - you won't believe what happens 😭"
    ],
}


EMOTION_KEYWORDS = {
    "apology": [
        "sorry",
        "apologize",
        "my bad",
        "i was wrong",
        "i shouldn't have"
    ],

    "emotional": [
        "cry",
        "crying",
        "heartbroken",
        "sad",
        "breaks down",
        "broke down"
    ],

    "career": [
        "made it",
        "can't believe this",
        "dream come true",
        "grateful",
        "blessed"
    ],

    "celebrity": [
        "you know who i am",
        "do you know who",
        "famous",
        "celebrity"
    ],

    "funny": [
        "lol",
        "haha",
        "no way",
        "bro what",
        "what just happened"
    ],

    "scared": [
        "scared",
        "i'm scared",
        "im scared",
        "terrified",
        "afraid",
        "fear"
    ],

    "francisco": [
        "francisco",
        "frankie"
    ],

    "stop": [
        "stop",
        "wait",
        "hold on",
        "hold up"
    ],
}


def pick_template_category(transcript):
    text = transcript.lower()

    # Check specific categories first (most specific first)
    priority_order = [
        "scared",      # "I'm scared, bro"
        "francisco",   # "Francisco" / "Frankie"
        "stop",        # "Stop" / "Wait"
        "apology",     # "sorry" / "apologize"
        "emotional",   # "cry" / "heartbroken"
        "career",      # "made it" / "dream come true"
        "celebrity",   # "famous" / "celebrity"
        "funny",       # "lol" / "haha"
    ]

    for category in priority_order:
        keywords = EMOTION_KEYWORDS.get(category, [])
        if any(keyword in text for keyword in keywords):
            return category

    return "default"


def generate_caption_template(
    transcript,
    streamer_name
):
    category = pick_template_category(
        transcript
    )

    template = random.choice(
        CAPTION_TEMPLATES[category]
    )

    name = (
        streamer_name
        or "the streamer"
    ).title()

    return template.format(
        streamer=name
    )


def generate_caption(
    transcript,
    streamer_name=None
):
    """
    Generate a caption from the actual transcript.

    Ollama is preferred.
    Template fallback is used if Ollama is unavailable.
    """

    if not transcript or not transcript.strip():

        return generate_caption_template(
            "",
            streamer_name
        )

    if _ollama_available():

        try:

            caption = generate_caption_with_ollama(
                transcript,
                streamer_name
            )

            if caption:

                print(
                    "Caption generated by Ollama.",
                    flush=True
                )

                return caption

        except Exception as error:

            print(
                "Ollama caption generation failed; "
                "using template fallback:",
                error,
                flush=True
            )

    print(
        "Using template caption fallback.",
        flush=True
    )

    return generate_caption_template(
        transcript,
        streamer_name
    )


def transcribe_and_caption(
    video_path,
    streamer_name=None
):
    """
    Full caption pipeline:

    VIDEO
      ↓
    WHISPER TRANSCRIPTION
      ↓
    FULL CAPTION GUIDE
      ↓
    OLLAMA ANALYSIS
      ↓
    SPECIFIC VIRAL CAPTION
    """

    print("=" * 60)
    print("CAPTION PIPELINE STARTED")
    print("=" * 60)

    print(
        "Transcribing actual video...",
        flush=True
    )

    transcript = transcribe_video(
        video_path
    )

    print(
        "Transcript:",
        flush=True
    )

    print(
        transcript[:2000],
        flush=True
    )

    print(
        "=" * 60
    )

    print(
        "Analyzing transcript with caption guide...",
        flush=True
    )

    caption = generate_caption(
        transcript,
        streamer_name
    )

    print("=" * 60)
    print("CAPTION PIPELINE FINISHED")
    print("=" * 60)

    print(
        "Generated caption:",
        caption,
        flush=True
    )

    return caption
