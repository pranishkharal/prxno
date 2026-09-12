"""
Metadata Generator - Creates searchable, accurate metadata for clips.

Generates:
- Title (accurate, not clickbait)
- Description (contextual)
- On-screen hook text
- Search phrases/keywords
- Caption (for TikTok/Reels)
- Hashtag suggestions
- All based on actual clip content
"""

import asyncio
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any
from pathlib import Path

from .config import CONFIG
from .context_analyzer import ExpandedMoment
from .moment_scorer import MomentScore
from .story_analyzer import StoryAnalysis
from .transcript_analyzer import TranscriptAnalysis
from .clip_ranker import RankedClip
from .caption_engine import (
    CaptionEngine,
    OnScreenCaptionGenerator,
    PostCaptionGenerator,
    detect_emotion_category,
)


@dataclass
class ClipMetadata:
    """Complete metadata for a clip."""
    title: str
    description: str
    on_screen_hook: str
    search_phrases: List[str]
    caption: str
    hashtags: List[str]
    streamer_name: str
    timestamp_display: str  # e.g., "1:23:45"
    rank: int = 0
    content_warnings: List[str] = field(default_factory=list)
    accuracy_score: float = 100.0  # How accurately metadata represents content
    on_screen_captions: List[Dict[str, Any]] = field(default_factory=list)
    post_caption_options: List[str] = field(default_factory=list)
    recommended_post_caption: str = ""
    search_terms: List[str] = field(default_factory=list)
    caption_style: str = "standard"
    confidence: float = 0.0


class MetadataGenerator:
    """
    Generates accurate, searchable metadata from clip analysis.
    """

    def __init__(self):
        self.ollama_available = self._check_ollama()

    def _check_ollama(self) -> bool:
        """Check if Ollama is available for LLM generation."""
        try:
            import requests
            resp = requests.get("http://localhost:11434", timeout=2)
            return resp.status_code < 500
        except Exception:
            return False

    async def generate_metadata(
        self,
        clip: RankedClip,
        transcript: TranscriptAnalysis,
        streamer_name: str,
        vod_title: str = "",
        vod_url: str = ""
    ) -> ClipMetadata:
        """
        Generate complete metadata for a clip.
        Uses LLM if available, falls back to template-based generation.
        """
        moment = clip.moment
        story = clip.story_analysis

        # Extract key information
        key_info = self._extract_key_info(moment, transcript, streamer_name)

        if self.ollama_available:
            return await self._generate_with_llm(clip, transcript, streamer_name, vod_title, key_info)
        else:
            return self._generate_template(clip, transcript, streamer_name, vod_title, key_info)

    def _extract_key_info(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis,
        streamer_name: str
    ) -> Dict[str, Any]:
        """Extract key information for metadata generation."""
        # Get relevant transcript segment
        segments = [
            s for s in transcript.segments
            if s.end > moment.expanded_start and s.start < moment.expanded_end
        ]
        clip_text = " ".join(s.text for s in segments)

        # Named entities
        entities = transcript.named_entities

        # Key phrases
        key_phrases = transcript.key_phrases

        # Topics
        topics = [t.get("topic", "") for t in transcript.topics[:3]]

        # Emotions
        emotions = [e.get("emotion", "") for e in transcript.emotions[:3]]

        return {
            "streamer": streamer_name,
            "clip_text": clip_text[:2000],
            "hook": moment.hook_text,
            "payoff": moment.payoff_text,
            "reaction": moment.reaction_text,
            "context": moment.context_text,
            "entities": entities,
            "key_phrases": key_phrases,
            "topics": topics,
            "emotions": emotions,
            "duration": moment.duration,
            "start_time": moment.expanded_start,
            "story_complete": moment.story_complete,
        }

    async def _generate_with_llm(
        self,
        clip: RankedClip,
        transcript: TranscriptAnalysis,
        streamer_name: str,
        vod_title: str,
        key_info: Dict[str, Any]
    ) -> ClipMetadata:
        """Generate metadata using Ollama LLM + deterministic caption engine."""
        prompt = self._build_metadata_prompt(key_info, vod_title)

        llm_title = ""
        llm_description = ""
        llm_search_phrases: List[str] = []
        llm_content_warnings: List[str] = []

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": CONFIG.transcript.llm_model,
                    "prompt": prompt,
                    "stream": False,
                    "options": {"temperature": 0.4}
                }
                async with session.post(
                    "http://localhost:11434/api/generate",
                    json=payload,
                    timeout=60
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        parsed = self._parse_llm_metadata_raw(data.get("response", ""), key_info)
                        llm_title = parsed.get("title", "")
                        llm_description = parsed.get("description", "")
                        llm_search_phrases = parsed.get("search_phrases", [])
                        llm_content_warnings = parsed.get("content_warnings", [])
        except Exception as e:
            print(f"LLM metadata generation failed: {e}")

        # Deterministic caption generation from actual transcript
        caption_engine = CaptionEngine()
        emotion_category = detect_emotion_category(
            " ".join([
                key_info.get("hook", ""),
                key_info.get("payoff", ""),
                key_info.get("reaction", ""),
            ]),
            key_info.get("clip_text", ""),
        )

        edit_intensity = "high_energy" if (clip.moment_score and clip.moment_score.emotion_score > 75) else "standard"
        if emotion_category in {"emotional", "serious", "wholesome"}:
            edit_intensity = "clean"

        on_screen_captions = caption_engine.generate_on_screen_captions(
            transcript_segments=transcript.segments,
            moment_start=key_info["start_time"],
            moment_end=key_info["start_time"] + key_info["duration"],
            emotion_category=emotion_category,
            style=edit_intensity if edit_intensity != "clean" else "clean",
        )

        post_captions = caption_engine.generate_post_captions(
            transcript_text=key_info.get("clip_text", ""),
            key_info=key_info,
            emotion_category=emotion_category,
        )

        title = llm_title or self._build_safe_title(key_info)
        description = llm_description or self._build_safe_description(key_info)

        return ClipMetadata(
            rank=clip.rank,
            title=title[:100],
            description=description[:500],
            on_screen_hook=on_screen_captions[0].text if on_screen_captions else "",
            search_phrases=llm_search_phrases or post_captions.search_terms[:8],
            caption=post_captions.recommended or post_captions.options[0] if post_captions.options else "",
            hashtags=post_captions.hashtags[:15],
            streamer_name=key_info["streamer"],
            timestamp_display=self._format_timestamp(key_info["start_time"]),
            content_warnings=llm_content_warnings or self._detect_content_warnings(clip, transcript),
            accuracy_score=95.0 if llm_title else 85.0,
            on_screen_captions=[
                {
                    "start": c.start,
                    "end": c.end,
                    "text": c.text,
                    "emphasis": c.emphasis,
                    "position": c.position,
                    "style": c.style,
                }
                for c in on_screen_captions
            ],
            post_caption_options=post_captions.options,
            recommended_post_caption=post_captions.recommended,
            search_terms=post_captions.search_terms,
            caption_style=edit_intensity,
            confidence=post_captions.confidence,
        )

    def _build_metadata_prompt(self, key_info: Dict[str, Any], vod_title: str) -> str:
        """Build prompt for LLM metadata generation."""
        return f"""You are a metadata generator for viral streamer clips on TikTok/Reels/Shorts.

Your job: Create ACCURATE, SEARCHABLE metadata based on the ACTUAL clip content.
DO NOT create clickbait, misleading titles, or invent events.

STREAMER: {key_info['streamer']}
VOD TITLE: {vod_title or "Unknown"}

CLIP CONTENT (with timestamps):
{key_info['clip_text']}

KEY MOMENTS:
- Hook: {key_info['hook']}
- Context: {key_info['context']}
- Payoff: {key_info['payoff']}
- Reaction: {key_info['reaction']}

ENTITIES MENTIONED: {', '.join([e.get('entity', '') for e in key_info['entities'][:5]])}
TOPICS: {', '.join(key_info['topics'])}
EMOTIONS: {', '.join(key_info['emotions'])}
DURATION: {key_info['duration']:.1f}s

Return JSON with these fields:
{{
  "title": "Accurate, descriptive title (max 100 chars)",
  "description": "2-3 sentence description of what happens",
  "on_screen_hook": "Text to show on screen in first 3 seconds (max 50 chars)",
  "search_phrases": ["phrase1", "phrase2", "phrase3"],  // What people would search to find this
  "caption": "TikTok/Reels caption (engaging but accurate, max 300 chars)",
  "hashtags": ["hashtag1", "hashtag2", ...],  // 5-10 relevant hashtags
  "content_warnings": []  // e.g., "loud_audio", "strong_language", "spoilers"
}}

Rules:
- Title must accurately describe the clip content
- Search phrases should be terms viewers would actually search
- Hashtags should be relevant to streamer, game, topic, platform
- No clickbait, no invented details, no misleading claims
- On-screen hook should match actual first words spoken"""

    def _parse_llm_metadata_raw(self, response: str, key_info: Dict[str, Any]) -> Dict[str, Any]:
        """Parse LLM response into a plain dict."""
        import json
        try:
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group())
            else:
                data = json.loads(response)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return {}

    def _build_safe_title(self, key_info: Dict[str, Any]) -> str:
        streamer = key_info.get("streamer", "Streamer")
        hook = (key_info.get("hook") or "").strip()
        payoff = (key_info.get("payoff") or "").strip()
        base = hook or payoff or "stream moment"
        title = f"{streamer}: {base}"
        return title[:100]

    def _build_safe_description(self, key_info: Dict[str, Any]) -> str:
        parts = []
        context = key_info.get("context")
        payoff = key_info.get("payoff")
        reaction = key_info.get("reaction")
        if context:
            parts.append(context[:150])
        if payoff:
            parts.append(payoff[:150])
        if reaction:
            parts.append(reaction[:150])
        description = " ".join(parts) if parts else f"{key_info.get('streamer', 'Streamer')} moment from stream."
        return description[:500]

    def _parse_llm_metadata(
        self,
        clip: RankedClip,
        response: str,
        key_info: Dict[str, Any]
    ) -> ClipMetadata:
        """Parse LLM response into ClipMetadata."""
        data = self._parse_llm_metadata_raw(response, key_info)

        caption_engine = CaptionEngine()
        emotion_category = detect_emotion_category(
            " ".join([
                key_info.get("hook", ""),
                key_info.get("payoff", ""),
                key_info.get("reaction", ""),
            ]),
            key_info.get("clip_text", ""),
        )

        edit_intensity = "high_energy" if (clip.moment_score and clip.moment_score.emotion_score > 75) else "standard"
        if emotion_category in {"emotional", "serious", "wholesome"}:
            edit_intensity = "clean"

        on_screen_captions = caption_engine.generate_on_screen_captions(
            transcript_segments=[],
            moment_start=key_info["start_time"],
            moment_end=key_info["start_time"] + key_info["duration"],
            emotion_category=emotion_category,
            style=edit_intensity if edit_intensity != "clean" else "clean",
        )

        post_captions = caption_engine.generate_post_captions(
            transcript_text=key_info.get("clip_text", ""),
            key_info=key_info,
            emotion_category=emotion_category,
        )

        return ClipMetadata(
            rank=clip.rank,
            title=data.get("title", "")[:100],
            description=data.get("description", "")[:500],
            on_screen_hook=data.get("on_screen_hook", "")[:50],
            search_phrases=data.get("search_phrases", [])[:10],
            caption=data.get("caption", "")[:300],
            hashtags=data.get("hashtags", [])[:15],
            streamer_name=key_info["streamer"],
            timestamp_display=self._format_timestamp(key_info["start_time"]),
            content_warnings=data.get("content_warnings", []),
            accuracy_score=95.0,
            on_screen_captions=[
                {
                    "start": c.start,
                    "end": c.end,
                    "text": c.text,
                    "emphasis": c.emphasis,
                    "position": c.position,
                    "style": c.style,
                }
                for c in on_screen_captions
            ],
            post_caption_options=post_captions.options,
            recommended_post_caption=post_captions.recommended,
            search_terms=post_captions.search_terms,
            caption_style=edit_intensity,
            confidence=post_captions.confidence,
        )

    def _generate_template(
        self,
        clip: RankedClip,
        transcript: TranscriptAnalysis,
        streamer_name: str,
        vod_title: str,
        key_info: Dict[str, Any]
    ) -> ClipMetadata:
        """Generate metadata using templates (fallback)."""
        moment = clip.moment

        caption_engine = CaptionEngine()
        emotion_category = detect_emotion_category(
            " ".join([
                key_info.get("hook", ""),
                key_info.get("payoff", ""),
                key_info.get("reaction", ""),
            ]),
            key_info.get("clip_text", ""),
        )

        edit_intensity = "high_energy" if (clip.moment_score and clip.moment_score.emotion_score > 75) else "standard"
        if emotion_category in {"emotional", "serious", "wholesome"}:
            edit_intensity = "clean"

        on_screen_captions = caption_engine.generate_on_screen_captions(
            transcript_segments=transcript.segments if transcript else [],
            moment_start=key_info["start_time"],
            moment_end=key_info["start_time"] + key_info["duration"],
            emotion_category=emotion_category,
            style=edit_intensity if edit_intensity != "clean" else "clean",
        )

        post_captions = caption_engine.generate_post_captions(
            transcript_text=key_info.get("clip_text", ""),
            key_info=key_info,
            emotion_category=emotion_category,
        )

        # Title - use hook or payoff
        if moment.hook_text and len(moment.hook_text) > 10:
            title_base = moment.hook_text[:80]
        elif moment.payoff_text and len(moment.payoff_text) > 10:
            title_base = moment.payoff_text[:80]
        else:
            title_base = f"{streamer_name} Reacts"

        title = f"{streamer_name}: {title_base}"
        if len(title) > 100:
            title = title[:97] + "..."

        # Description
        desc_parts = []
        if moment.context_text:
            desc_parts.append(moment.context_text[:150])
        if moment.payoff_text:
            desc_parts.append(moment.payoff_text[:150])
        description = " ".join(desc_parts) if desc_parts else f"{streamer_name} moment from stream."

        # Search phrases - combine entities, topics, streamer
        search_phrases = [streamer_name]
        search_phrases.extend([e.get("entity", "") for e in key_info["entities"][:3] if e.get("entity")])
        search_phrases.extend(key_info["topics"][:2])
        search_phrases.extend(key_info["key_phrases"][:2])
        search_phrases = [p for p in search_phrases if p][:8]

        return ClipMetadata(
            rank=clip.rank,
            title=title[:100],
            description=description[:500],
            on_screen_hook=on_screen_captions[0].text if on_screen_captions else "",
            search_phrases=search_phrases,
            caption=post_captions.recommended or post_captions.options[0] if post_captions.options else "",
            hashtags=post_captions.hashtags[:15],
            streamer_name=streamer_name,
            timestamp_display=self._format_timestamp(key_info["start_time"]),
            content_warnings=self._detect_content_warnings(clip, transcript),
            accuracy_score=85.0,
            on_screen_captions=[
                {
                    "start": c.start,
                    "end": c.end,
                    "text": c.text,
                    "emphasis": c.emphasis,
                    "position": c.position,
                    "style": c.style,
                }
                for c in on_screen_captions
            ],
            post_caption_options=post_captions.options,
            recommended_post_caption=post_captions.recommended,
            search_terms=post_captions.search_terms,
            caption_style=edit_intensity,
            confidence=post_captions.confidence,
        )

    def _generate_template_fallback(self, key_info: Dict[str, Any]) -> ClipMetadata:
        """Minimal fallback when everything fails."""
        streamer = key_info.get("streamer", "Streamer")
        return ClipMetadata(
            title=f"{streamer} Stream Moment",
            description=f"Clip from {streamer}'s stream.",
            on_screen_hook=f"{streamer} reacts",
            search_phrases=[streamer],
            caption=f"{streamer} moment 😭",
            hashtags=[streamer.lower(), "streamer", "clips", "twitch", "kick"],
            streamer_name=streamer,
            timestamp_display="0:00",
            content_warnings=[],
            accuracy_score=50.0,
            on_screen_captions=[],
            post_caption_options=[],
            recommended_post_caption="",
            search_terms=[streamer],
            caption_style="standard",
            confidence=40.0,
        )

    def _generate_caption_template(
        self,
        moment: ExpandedMoment,
        streamer_name: str,
        key_info: Dict[str, Any]
    ) -> str:
        """Generate caption using template based on detected emotion/type."""
        # Determine type from content
        text = (moment.hook_text + moment.payoff_text + moment.reaction_text).lower()

        if any(w in text for w in ["sorry", "apologize", "my bad", "wrong"]):
            template = "the wholesome moment {streamer} showed maturity and apologized ❤️‍🩹"
        elif any(w in text for w in ["cry", "crying", "heartbroken", "sad", "tears"]):
            template = "the emotional moment {streamer} realized what happened ❤️‍🩹"
        elif any(w in text for w in ["made it", "dream come true", "can't believe", "grateful"]):
            template = "{streamer} had their \"I MADE IT\" moment ❤️‍🩹"
        elif any(w in text for w in ["lol", "lmao", "haha", "funny", "joke"]):
            template = "{streamer} did NOT expect that to happen 😭"
        elif any(w in text for w in ["insane", "crazy", "unbelievable", "wtf", "omg", "no way"]):
            template = "{streamer} reacts to an INSANE moment 😱"
        else:
            template = "{streamer} reacts live - you won't believe what happens 😭"

        caption = template.format(streamer=streamer_name)

        # Add relevant hashtags inline
        entities = [e.get("entity", "") for e in key_info["entities"][:2] if e.get("entity")]
        if entities:
            caption += " " + " ".join(f"#{e.replace(' ', '')}" for e in entities)

        return caption

    def _generate_hashtags(
        self,
        streamer_name: str,
        key_info: Dict[str, Any]
    ) -> List[str]:
        """Generate relevant hashtags."""
        tags = [
            streamer_name.lower().replace(" ", ""),
            "streamer",
            "clips",
            "kick",
            "kickstreamer",
            "livestream",
            "gaming",
            "viral",
            "fyp",
            "foryou",
        ]

        # Add entity-based tags
        for entity in key_info["entities"][:3]:
            entity_name = entity.get("entity", "").lower().replace(" ", "")
            if entity_name and len(entity_name) > 2:
                tags.append(entity_name)

        # Add topic tags
        for topic in key_info["topics"][:2]:
            topic_tag = topic.lower().replace(" ", "")
            if topic_tag:
                tags.append(topic_tag)

        # Add emotion tags
        for emotion in key_info["emotions"][:2]:
            tags.append(emotion.lower())

        # Deduplicate and limit
        seen = set()
        unique = []
        for tag in tags:
            clean = re.sub(r'[^a-z0-9]', '', tag.lower())
            if clean and clean not in seen and len(clean) > 1:
                seen.add(clean)
                unique.append(f"#{clean}")

        return unique[:15]

    def _detect_content_warnings(
        self,
        clip: RankedClip,
        transcript: TranscriptAnalysis
    ) -> List[str]:
        """Detect content warnings for the clip."""
        moment = clip.moment
        warnings = []
        text = (moment.hook_text + moment.context_text + moment.payoff_text + moment.reaction_text).lower()

        # Loud audio
        if clip.moment_score and clip.moment_score.emotion_score > 80:
            warnings.append("loud_audio")

        # Strong language
        profanity = ["fuck", "shit", "bitch", "ass", "damn", "hell"]
        if any(w in text for w in profanity):
            warnings.append("strong_language")

        # Spoilers (if discussing media)
        spoiler_words = ["spoiler", "ending", "dies", "death", "twist", "reveal"]
        if any(w in text for w in spoiler_words):
            warnings.append("potential_spoilers")

        return warnings

    def _format_timestamp(self, seconds: float) -> str:
        """Format seconds as H:MM:SS or M:SS."""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        if hours > 0:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        else:
            return f"{minutes}:{secs:02d}"


async def generate_metadata_for_all_clips(
    ranked_clips: List[RankedClip],
    transcript: TranscriptAnalysis,
    streamer_name: str,
    vod_title: str = "",
    vod_url: str = ""
) -> List[ClipMetadata]:
    """Generate metadata for multiple clips (sequential to avoid rate limits)."""
    generator = MetadataGenerator()
    results = []

    for clip in ranked_clips:
        # Run sequentially to avoid overwhelming Ollama
        metadata = await generator.generate_metadata(
            clip, transcript, streamer_name, vod_title, vod_url
        )
        results.append(metadata)

    return results