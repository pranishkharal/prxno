"""
Caption Engine - Generates both on-screen captions and TikTok post captions.

Architecture:
    transcript + moment/emotion context
        ↓
    CaptionEngine
        ├── OnScreenCaptionGenerator
        │   ├── clean_transcript()
        │   ├── chunk_captions()
        │   ├── identify_important_words()
        │   └── build_timed_captions()
        │
        └── PostCaptionGenerator
            ├── detect_emotion_category()
            ├── generate_searchable_caption()
            ├── generate_curious_caption()
            ├── generate_dramatic_caption()
            ├── extract_search_terms()
            └── generate_hashtags()

Principles:
- On-screen captions come from actual spoken words only.
- Post captions describe the actual moment accurately.
- Never invent quotes, names, or events.
- Never use misleading clickbait.
"""

from __future__ import annotations

import json
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class CaptionSegment:
    """One on-screen caption chunk."""
    start: float
    end: float
    text: str
    emphasis: List[str] = field(default_factory=list)
    position: str = "bottom_center"
    style: str = "standard"


@dataclass
class PostCaptionResult:
    """TikTok/Reels post caption output."""
    options: List[str] = field(default_factory=list)
    recommended: str = ""
    search_terms: List[str] = field(default_factory=list)
    hashtags: List[str] = field(default_factory=list)
    confidence: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FILLER_WORDS = {
    "um", "uh", "like", "you know", "i mean", "basically", "literally",
    "actually", "right", "okay", "ok", "yeah", "y'know"
}

_EMOTION_KEYWORDS: Dict[str, List[str]] = {
    "funny": ["lol", "haha", "lmao", "rofl", "funny", "joke", "hilarious", "dying", "dead"],
    "shocking": ["no way", "wtf", "omg", "what the", "shocking", "unbelievable", "insane", "no way"],
    "emotional": ["cry", "crying", "heartbroken", "sad", "tears", "emotional", "breaks down", "broke down"],
    "angry": ["pissed", "angry", "furious", "mad", "rage", "screaming", "yelling"],
    "dramatic": ["drama", "dramatic", "chaos", " chaotic", "explosive", "insane"],
    "confusing": ["wait what", "confused", "confusing", "what happened", "i don't understand"],
    "exciting": ["lets go", "lets go", "pog", "poggers", "hype", "lets gooo", "lets go"],
    "controversial": ["controversy", "controversial", "cancel", "clout", "exposed", "expose"],
    "wholesome": ["wholesome", "sweet", "adorable", "heartwarming", "pure"],
    "serious": ["serious", "listen", "important", "real", "honest"],
    "casual": ["chill", "casual", "vibes", "laid back"],
}


def _clean_transcript_text(text: str) -> str:
    """Remove obvious filler words while preserving meaning."""
    words = text.split()
    cleaned = []
    for word in words:
        lower = word.lower().strip(".,!?")
        if lower in _FILLER_WORDS:
            continue
        cleaned.append(word)
    return " ".join(cleaned)


def _chunk_phrase(text: str, max_words: int = 6) -> List[str]:
    """Break transcript text into short natural phrases."""
    words = text.split()
    chunks = []
    for i in range(0, len(words), max_words):
        chunk = words[i:i + max_words]
        if chunk:
            chunks.append(" ".join(chunk))
    return chunks


def _identify_important_words(text: str) -> List[str]:
    """
    Return words/phrases that carry emotional or informational weight.
    Keeps the result small and selective.
    """
    candidates = re.findall(r"[A-Za-z']+", text)
    important = []
    seen = set()
    for word in candidates:
        lower = word.lower()
        if lower in seen:
            continue
        if len(lower) < 3:
            continue
        # Words that are often emotionally weighted in streamer clips
        if lower in {
            "no", "yes", "stop", "wait", "help", "why", "what", "how",
            "can't", "won't", "never", "always", "believe", "thought",
            "actually", "really", "truth", "lie", "lying", "scared",
            "terrified", "love", "hate", "angry", "mad", "crying",
            "cry", "heartbroken", "shocked", "insane", "crazy",
        }:
            important.append(word)
            seen.add(lower)
    return important[:5]


def detect_emotion_category(moment_text: str, transcript_text: str) -> str:
    """Detect the dominant emotion category from the moment."""
    combined = f"{moment_text} {transcript_text}".lower()
    scores: Dict[str, int] = {}
    for category, keywords in _EMOTION_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in combined)
        if score:
            scores[category] = score
    if not scores:
        return "casual"
    return max(scores, key=lambda k: scores[k])


# ---------------------------------------------------------------------------
# On-screen caption generator
# ---------------------------------------------------------------------------

class CaptionEngine:
    """
    High-level caption engine that coordinates on-screen and post captions.

    Usage:
        engine = CaptionEngine(style="standard")
        on_screen = engine.generate_on_screen_captions(...)
        post = engine.generate_post_captions(...)
    """

    def __init__(
        self,
        style: str = "standard",
        ollama_url: str = "http://localhost:11434/api/generate",
        ollama_model: str = "llama3.2",
    ):
        self.style = style
        self.on_screen_generator = OnScreenCaptionGenerator(style=style)
        self.post_generator = PostCaptionGenerator(ollama_url=ollama_url, ollama_model=ollama_model)

    def generate_on_screen_captions(
        self,
        transcript_segments: List[Any],
        moment_start: float,
        moment_end: float,
        emotion_category: str,
        style: Optional[str] = None,
    ) -> List[CaptionSegment]:
        """Generate timed on-screen captions from actual transcript."""
        return self.on_screen_generator.generate(
            transcript_segments=transcript_segments,
            moment_start=moment_start,
            moment_end=moment_end,
            emotion_category=emotion_category,
        )

    def generate_post_captions(
        self,
        transcript_text: str,
        key_info: Dict[str, Any],
        emotion_category: str,
        edit_intensity: str = "standard",
    ) -> PostCaptionResult:
        """Generate TikTok/Reels post captions."""
        return self.post_generator.generate(
            transcript_text=transcript_text,
            key_info=key_info,
            emotion_category=emotion_category,
            edit_intensity=edit_intensity,
        )


class OnScreenCaptionGenerator:
    """
    Generates timed on-screen captions from actual transcript segments.
    """

    def __init__(self, style: str = "standard"):
        self.style = style

    def generate(
        self,
        transcript_segments: List[Any],
        moment_start: float,
        moment_end: float,
        emotion_category: str,
    ) -> List[CaptionSegment]:
        """
        Build on-screen captions only from real transcript words
        that fall inside the clip window.
        """
        if not transcript_segments:
            return []

        segments = [
            s for s in transcript_segments
            if getattr(s, "end", 0) > moment_start
            and getattr(s, "start", 0) < moment_end
        ]

        if not segments:
            return []

        captions: List[CaptionSegment] = []
        position = self._pick_position(emotion_category)

        for seg in segments:
            raw_text = getattr(seg, "text", "") or ""
            if not raw_text.strip():
                continue

            cleaned = _clean_transcript_text(raw_text)
            if not cleaned:
                continue

            chunks = _chunk_phrase(cleaned, max_words=6)
            if not chunks:
                continue

            seg_start = max(float(getattr(seg, "start", 0)), moment_start)
            seg_end = min(float(getattr(seg, "end", 0)), moment_end)
            chunk_duration = max(0.8, (seg_end - seg_start) / len(chunks))

            for idx, chunk in enumerate(chunks):
                c_start = seg_start + idx * chunk_duration
                c_end = min(c_start + chunk_duration, seg_end)
                emphasis = _identify_important_words(chunk) if self.style != "clean" else []
                captions.append(
                    CaptionSegment(
                        start=round(c_start, 2),
                        end=round(c_end, 2),
                        text=chunk.upper() if self.style == "high_energy" else chunk,
                        emphasis=emphasis,
                        position=position,
                        style=self.style,
                    )
                )

        return captions

    def _pick_position(self, emotion_category: str) -> str:
        """
        Default caption placement. In a full implementation this can
        inspect frame regions to avoid faces/HUD; here we keep it safe.
        """
        if emotion_category in {"shocking", "exciting", "dramatic"}:
            return "top_center"
        return "bottom_center"


# ---------------------------------------------------------------------------
# Post caption generator
# ---------------------------------------------------------------------------

class PostCaptionGenerator:
    """
    Generates TikTok/Reels post captions from actual clip content.
    Never invents quotes or events. Always grounded in transcript.
    """

    def __init__(self, ollama_url: str = "http://localhost:11434/api/generate", ollama_model: str = "llama3.2"):
        self.ollama_url = ollama_url
        self.ollama_model = ollama_model
        self._ollama_available = self._check_ollama()

    def _check_ollama(self) -> bool:
        try:
            import requests
            resp = requests.get("http://localhost:11434", timeout=1.5)
            return resp.status_code < 500
        except Exception:
            return False

    def generate(
        self,
        transcript_text: str,
        key_info: Dict[str, Any],
        emotion_category: str,
        edit_intensity: str = "standard",
    ) -> PostCaptionResult:
        """
        Generate 3 post caption options:
        - searchable
        - curious
        - dramatic

        All must be accurate, non-misleading, and based on real transcript content.
        """
        cleaned_transcript = _clean_transcript_text(transcript_text)
        streamer = key_info.get("streamer", "Streamer")
        clip_text = key_info.get("clip_text", cleaned_transcript[:600])

        if not cleaned_transcript or len(cleaned_transcript.split()) < 3:
            return self._fallback(streamer, key_info, emotion_category)

        searchable = self._generate_searchable(streamer, clip_text, emotion_category, key_info)
        curious = self._generate_curious(streamer, clip_text, emotion_category, key_info)
        dramatic = self._generate_dramatic(streamer, clip_text, emotion_category, key_info)

        options = [searchable, curious, dramatic]
        recommended = self._pick_recommended(options, emotion_category, edit_intensity)
        search_terms = self._extract_search_terms(streamer, key_info, emotion_category)
        hashtags = self._generate_hashtags(streamer, key_info, emotion_category)
        confidence = self._estimate_confidence(clip_text, key_info)

        return PostCaptionResult(
            options=options,
            recommended=recommended,
            search_terms=search_terms,
            hashtags=hashtags,
            confidence=confidence,
        )

    # ------------------------------------------------------------------
    # Caption strategies
    # ------------------------------------------------------------------

    def _generate_searchable(self, streamer: str, clip_text: str, emotion: str, key_info: Dict[str, Any]) -> str:
        hook = (key_info.get("hook") or "").strip()
        payoff = (key_info.get("payoff") or "").strip()
        topic = (key_info.get("topics") or [""])[0]
        entities = [e.get("entity", "") for e in (key_info.get("entities") or [])[:2] if e.get("entity")]

        # Build from actual facts only
        parts = []
        if entities:
            parts.append(entities[0])
        if topic:
            parts.append(topic)
        if hook:
            parts.append(hook)
        elif payoff:
            parts.append(payoff)

        text = " ".join(parts) if parts else clip_text[:140]
        text = f"{streamer} - {text}" if streamer.lower() not in text.lower() else text
        return text[:300]

    def _generate_curious(self, streamer: str, clip_text: str, emotion: str, key_info: Dict[str, Any]) -> str:
        hook = (key_info.get("hook") or "").strip()
        payoff = (key_info.get("payoff") or "").strip()
        topic = (key_info.get("topics") or [""])[0]

        if not hook and not payoff:
            return f"{streamer} shares what happened during {topic or 'the stream'}." if topic else f"{streamer} reacts to an unexpected moment."

        if emotion in {"shocking", "exciting"}:
            return f"{streamer} didn't see this coming: {hook or payoff}"
        if emotion == "emotional":
            return f"{streamer} opens up about what really happened."
        if emotion == "funny":
            return f"{streamer} couldn't keep it together when {hook.lower() if hook else 'this happened'}."
        return f"{streamer} reveals what happened during {topic or 'the stream'}."

    def _generate_dramatic(self, streamer: str, clip_text: str, emotion: str, key_info: Dict[str, Any]) -> str:
        hook = (key_info.get("hook") or "").strip()
        payoff = (key_info.get("payoff") or "").strip()
        reaction = (key_info.get("reaction") or "").strip()
        emotion_map = {
            "shocking": "shocked",
            "emotional": "emotional",
            "angry": "frustrated",
            "funny": "uncontrollable",
            "exciting": "hyped",
            "controversial": "speechless",
        }
        feeling = emotion_map.get(emotion, "reacting")

        base = hook or payoff or reaction or "this moment"
        return f"{streamer} was {feeling} after {base.lower()}"

    def _pick_recommended(self, options: List[str], emotion: str, edit_intensity: str) -> str:
        if edit_intensity == "high_energy":
            return options[2] if len(options) > 2 else options[0]
        if emotion in {"emotional", "serious", "wholesome"}:
            return options[0]
        return options[1]

    # ------------------------------------------------------------------
    # Search terms / hashtags
    # ------------------------------------------------------------------

    def _extract_search_terms(self, streamer: str, key_info: Dict[str, Any], emotion: str) -> List[str]:
        terms = [streamer]
        entities = [e.get("entity", "") for e in (key_info.get("entities") or []) if e.get("entity")]
        topics = key_info.get("topics") or []
        key_phrases = key_info.get("key_phrases") or []

        terms.extend(entities[:3])
        terms.extend(topics[:2])
        terms.extend(key_phrases[:2])

        if emotion and emotion != "casual":
            terms.append(emotion)

        seen = set()
        unique = []
        for term in terms:
            clean = term.strip()
            if clean and clean.lower() not in seen:
                seen.add(clean.lower())
                unique.append(clean)
        return unique[:10]

    def _generate_hashtags(self, streamer: str, key_info: Dict[str, Any], emotion: str) -> List[str]:
        base = [
            streamer.lower().replace(" ", ""),
            "streamer",
            "clips",
            "kick",
            "livestream",
            "gaming",
            "fyp",
        ]
        entities = [e.get("entity", "").lower().replace(" ", "") for e in (key_info.get("entities") or []) if e.get("entity")]
        topics = [t.lower().replace(" ", "") for t in (key_info.get("topics") or []) if t]
        emotions = [emotion.lower()] if emotion and emotion != "casual" else []

        candidates = base + entities[:3] + topics[:2] + emotions
        seen = set()
        tags = []
        for tag in candidates:
            clean = re.sub(r"[^a-z0-9]", "", tag.lower())
            if clean and clean not in seen and len(clean) > 1:
                seen.add(clean)
                tags.append(f"#{clean}")
        return tags[:12]

    # ------------------------------------------------------------------
    # Confidence / fallback
    # ------------------------------------------------------------------

    def _estimate_confidence(self, clip_text: str, key_info: Dict[str, Any]) -> float:
        word_count = len(clip_text.split())
        if word_count < 5:
            return 40.0
        if word_count < 15:
            return 65.0
        entities = key_info.get("entities") or []
        topics = key_info.get("topics") or []
        if entities or topics:
            return 92.0
        return 80.0

    def _fallback(self, streamer: str, key_info: Dict[str, Any], emotion: str) -> PostCaptionResult:
        topic = (key_info.get("topics") or ["stream moment"])[0]
        option_a = f"{streamer} - {topic} moment from the stream."
        option_b = f"{streamer} reacts to what happened during {topic}."
        option_c = f"{streamer} was caught off guard by {topic}."
        return PostCaptionResult(
            options=[option_a, option_b, option_c],
            recommended=option_b,
            search_terms=[streamer, topic],
            hashtags=[streamer.lower().replace(" ", ""), "clips", "kick", "fyp"],
            confidence=50.0,
        )
