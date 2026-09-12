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


def generate_caption_with_ollama(
    transcript,
    streamer_name
):
    """
    Use the transcript + full caption guide
    to generate a specific caption for this clip.
    """

    guide = CAPTION_STYLE_GUIDE

    prompt = f"""
You are an expert viral streamer-clip caption writer.

Your job is NOT to produce the same generic caption for every creator.

You MUST analyze the actual transcript first.

Identify what is actually happening in THIS clip:

- What did the streamer say or react to?
- What is surprising?
- What is emotional?
- Is there a conflict?
- Is there an apology?
- Is there a famous person?
- Is there a career milestone?
- Is there money involved?
- Is there a consequence?
- Is there a funny or unexpected moment?
- What detail would make someone want to keep watching?
- What detail could make people comment?

Then apply the caption guide below.

========================
FULL CAPTION GUIDE
========================

{guide}

========================
STREAMER
========================

{streamer_name or "the streamer"}

========================
ACTUAL VIDEO TRANSCRIPT
========================

{transcript}

========================
IMPORTANT RULES
========================

1. Base the caption on the ACTUAL transcript.

2. Do not invent events.

3. Do not invent names.

4. Do not invent money amounts.

5. Do not invent consequences.

6. If the transcript contains a useful specific detail,
   prefer that detail over a generic phrase.

7. Use the caption strategies from the guide.

8. Do NOT blindly reuse the same template.

9. Different videos should produce different captions
   when their actual content is different.

10. Create curiosity without falsely revealing everything.

11. Use emotional or dramatic wording when appropriate.

12. Use important names, numbers, objects and consequences
   when they are actually supported by the transcript.

13. If a celebrity or recognizable person is involved,
   follow the guide's mystery strategy when appropriate.

14. Use trigger words from the guide naturally.

15. Return ONE caption only.

16. Do not explain your reasoning.

17. Do not write "Caption:".

18. Do not use hashtags unless the guide specifically requires them.

19. Keep it suitable for TikTok / Instagram Reels / YouTube Shorts.

20. The caption should sound like a real viral streamer clip page.

========================
OUTPUT
========================

Return ONLY the final caption.
"""

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.8
            }
        },
        timeout=120
    )

    response.raise_for_status()

    data = response.json()

    caption = data.get(
        "response",
        ""
    ).strip()

    caption = caption.strip(
        '"'
    ).strip()

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
}


def pick_template_category(transcript):
    text = transcript.lower()

    for category, keywords in EMOTION_KEYWORDS.items():

        if any(
            keyword in text
            for keyword in keywords
        ):
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
