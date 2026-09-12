"""
Local AI caption generation for the Auto Edit bot.

Flow:
    Video
      -> Whisper transcription
      -> transcript analysis by Ollama
      -> select the appropriate caption strategy
      -> generate a unique ready-to-post caption

The caption strategy is stored separately in caption_guide.json.
"""

import json
import requests
import whisper
from pathlib import Path


_WHISPER_MODEL = None

BASE_DIR = Path(__file__).resolve().parent
CAPTION_GUIDE_PATH = BASE_DIR / "caption_guide.json"

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2"


def get_whisper_model():
    """
    Lazily load Whisper once and reuse it.
    """
    global _WHISPER_MODEL

    if _WHISPER_MODEL is None:
        print("Loading local Whisper model (first run may take a moment)...")
        _WHISPER_MODEL = whisper.load_model("base")

    return _WHISPER_MODEL


def load_caption_guide():
    """
    Load the caption strategy/rules from caption_guide.json.
    """
    try:
        with open(CAPTION_GUIDE_PATH, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as error:
        print("Could not load caption_guide.json:", error)
        return {}


def transcribe_video(video_path):
    """
    Transcribe the actual speech from the video using local Whisper.
    """
    model = get_whisper_model()

    print("Transcribing video with Whisper...")

    result = model.transcribe(
        str(video_path),
        fp16=False
    )

    transcript = result.get("text", "").strip()

    print(
        f"Whisper transcription complete: "
        f"{len(transcript)} characters"
    )

    return transcript


def _ollama_available():
    try:
        requests.get(
            "http://localhost:11434",
            timeout=1.5
        )
        return True
    except Exception:
        return False


def _build_strategy_text(guide):
    """
    Convert the JSON guide into a clear instruction block for Ollama.
    """
    if not guide:
        return ""

    lines = []

    lines.append("CAPTION PURPOSE:")
    lines.append(guide.get("purpose", ""))

    lines.append("\nCORE PRINCIPLES:")
    for item in guide.get("core_principles", []):
        lines.append(f"- {item}")

    lines.append("\nVIEWER EFFECT:")
    for item in guide.get("viewer_effect", []):
        lines.append(f"- {item}")

    lines.append("\nCAPTION STRATEGIES:")

    strategies = guide.get("strategies", {})

    for name, data in strategies.items():
        lines.append(f"\n[{name.upper()}]")
        lines.append(f"When: {data.get('when', '')}")

        for rule in data.get("rules", []):
            lines.append(f"- {rule}")

        templates = data.get("templates", [])
        if templates:
            lines.append("Starter formats:")
            for template in templates:
                lines.append(f"- {template}")

        if data.get("example"):
            lines.append(f"Example: {data['example']}")

    lines.append("\nTRIGGER WORDS:")
    lines.append(
        ", ".join(guide.get("trigger_words", []))
    )

    lines.append("\nTRIGGER WORD RULES:")
    for item in guide.get("trigger_word_rules", []):
        lines.append(f"- {item}")

    lines.append("\nOUTPUT RULES:")
    for item in guide.get("output_rules", []):
        lines.append(f"- {item}")

    return "\n".join(lines)


def generate_caption_with_ollama(transcript, streamer_name):
    """
    Ask Ollama to analyze the actual transcript and create
    a caption using the appropriate strategy from the guide.
    """

    guide = load_caption_guide()
    strategy_text = _build_strategy_text(guide)

    streamer = streamer_name or "the streamer"

    prompt = f"""
You are an expert social-media editor for streamer clips.

Your job is NOT to blindly use one caption template.

You must analyze the actual transcript first.

Then:

1. Determine what actually happened in the clip.
2. Identify the strongest interesting moment.
3. Identify the emotion or viewer hook.
4. Decide which caption strategy from the guide best matches the moment.
5. Extract important factual details from the transcript.
6. Decide whether mystery, specific context, numbers, consequences,
   emotional wording, humor, or a trigger word would improve the caption.
7. Write ONE ready-to-post caption specifically for THIS clip.

IMPORTANT:
The caption must be based on the transcript.
Never invent events, names, people, numbers, outcomes or emotions.
Do not use the same generic wording for every streamer.
Do not automatically choose the default strategy.
Do not force a trigger word if it does not fit.

CAPTION GUIDE
==============
{strategy_text}
==============

CURRENT STREAMER:
{streamer}

ACTUAL VIDEO TRANSCRIPT:
========================
{transcript}
========================

Before writing internally determine:
- What happened?
- What is the most interesting part?
- What emotion is present?
- Which strategy fits?
- Which details should be included?
- Which details should remain mysterious?
- Is a trigger word appropriate?

Then output ONLY the final caption.

No explanation.
No analysis.
No hashtags.
No labels.
No quotation marks around the entire answer.
"""

    response = requests.post(
        OLLAMA_URL,
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.85
            }
        },
        timeout=120
    )

    response.raise_for_status()

    caption = response.json().get(
        "response",
        ""
    ).strip()

    caption = caption.strip('"').strip()

    if not caption:
        return None

    return caption


def generate_caption_template(transcript, streamer_name):
    """
    Emergency fallback if Ollama is unavailable.

    This deliberately uses transcript-derived wording instead of
    always returning the old generic caption.
    """

    name = streamer_name or "the streamer"

    if transcript and transcript.strip():
        text = " ".join(transcript.split())

        if len(text) > 160:
            text = text[:157].rstrip() + "..."

        return f"{name} just had this moment on stream 👀"

    return f"{name} just had an unexpected moment on stream 👀"


def generate_caption(transcript, streamer_name=None):
    """
    Generate a caption from the actual transcript.
    """

    if not transcript or not transcript.strip():
        print("No transcript detected. Using fallback caption.")
        return generate_caption_template(
            "",
            streamer_name
        )

    if _ollama_available():
        try:
            print("Analyzing transcript with Ollama...")

            caption = generate_caption_with_ollama(
                transcript,
                streamer_name
            )

            if caption:
                print(
                    "AI caption generated:",
                    caption
                )
                return caption

        except Exception as error:
            print(
                "Ollama caption generation failed:",
                error
            )
            print(
                "Using fallback caption."
            )

    else:
        print(
            "Ollama is not running. "
            "Using fallback caption."
        )

    return generate_caption_template(
        transcript,
        streamer_name
    )


def transcribe_and_caption(video_path, streamer_name=None):
    """
    Full caption pipeline:

    video -> Whisper transcript -> AI analysis -> caption
    """

    print("=" * 60)
    print("CAPTION PIPELINE STARTED")
    print("=" * 60)

    transcript = transcribe_video(
        video_path
    )

    print("Transcript:")
    print(transcript[:1000])

    caption = generate_caption(
        transcript,
        streamer_name
    )

    print("=" * 60)
    print("CAPTION PIPELINE FINISHED")
    print("=" * 60)

    return caption
