"""
Edit Planner - Content-aware editing decisions for each clip.

Determines optimal editing parameters based on clip content:
- Aspect ratio and cropping
- Caption style and necessity
- Silence/dead space removal
- Visual enhancements
- Background treatment
- Special effects (zoom, mirror, split screen)
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path

from .config import CONFIG, EditConfig
from .context_analyzer import ExpandedMoment
from .moment_scorer import MomentScore
from .story_analyzer import StoryAnalysis
from .transcript_analyzer import TranscriptAnalysis


@dataclass
class EditPlan:
    """Complete edit plan for a clip."""
    # Core editing options (matching main.py EditView options)
    size: str = "9:16"
    zoom: bool = False
    mirror: bool = False
    blur: bool = False
    remove_silence: bool = True
    overlay: bool = True
    enhance: str = "Off"
    split_screen: bool = False
    auto_captions: bool = False

    # Advanced options
    caption_style: str = "word_by_word"  # "word_by_word", "sentence_by_sentence", "none"
    silence_aggressiveness: str = "moderate"  # "light", "moderate", "aggressive"
    preserve_pauses: List[Tuple[float, float]] = field(default_factory=list)
    custom_crop: Optional[str] = None  # FFmpeg crop expression
    color_grade: Optional[str] = None  # LUT or color grade preset

    # Clip structure analysis
    clip_type: str = "other"
    hook_start: float = 0.0
    hook_description: str = ""
    context_required: bool = True
    context_description: str = ""
    payoff_timestamp: float = 0.0
    payoff_description: str = ""
    ending_timestamp: float = 0.0
    ending_description: str = ""

    # Editing intensity
    editing_intensity: str = "moderate"  # "natural", "moderate", "high_energy"

    # Audio
    audio_processing: str = "none"  # "none", "normalize", "enhance", "noise_reduce"

    # Confidence scores
    hook_confidence: float = 0.5
    context_confidence: float = 0.5
    caption_confidence: float = 0.5
    split_screen_confidence: float = 0.5

    # Metadata for editor
    reasoning: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_options_dict(self) -> Dict[str, Any]:
        """Convert to options dict compatible with main.py edit_video."""
        return {
            "size": self.size,
            "zoom": self.zoom,
            "mirror": self.mirror,
            "blur": self.blur,
            "remove_silence": self.remove_silence,
            "overlay": self.overlay,
            "enhance": self.enhance,
            "split_screen": self.split_screen,
            "auto_captions": self.auto_captions,
            "caption_style": self.caption_style,
            "silence_aggressiveness": self.silence_aggressiveness,
            "preserve_pauses": self.preserve_pauses,
            "custom_crop": self.custom_crop,
            "color_grade": self.color_grade,
            "editing_intensity": self.editing_intensity,
            "audio_processing": self.audio_processing,
            "hook_start": self.hook_start,
            "payoff_timestamp": self.payoff_timestamp,
            "ending_timestamp": self.ending_timestamp,
            "hook_confidence": self.hook_confidence,
            "context_confidence": self.context_confidence,
            "caption_confidence": self.caption_confidence,
            "split_screen_confidence": self.split_screen_confidence,
            "reasoning": self.reasoning,
            "warnings": self.warnings,
        }


class EditPlanner:
    """
    Creates content-aware edit plans for clips.
    """

    def __init__(self, config: EditConfig = None):
        self.config = config or CONFIG.edit

    def create_plan(
        self,
        moment: ExpandedMoment,
        moment_score: MomentScore,
        story: StoryAnalysis,
        transcript: TranscriptAnalysis,
        streamer_name: str = "",
        vod_title: str = ""
    ) -> EditPlan:
        """
        Generate an edit plan based on clip content analysis.

        The editor should be INTELLIGENT rather than "ENABLE EVERYTHING = BETTER VIDEO"
        """
        plan = EditPlan()

        # 0. Detect clip type
        plan.clip_type = self._detect_clip_type(moment, transcript, moment_score)
        plan.reasoning["clip_type"] = f"Detected clip type: {plan.clip_type}"

        # 1. Aspect ratio - always 9:16 for TikTok/Reels/Shorts
        plan.size = "9:16"
        plan.reasoning["size"] = "Vertical format for short-form platforms"

        # 2. Captions - based on speech density and duration
        plan.auto_captions, plan.caption_style = self._decide_captions(moment, transcript)
        plan.caption_confidence = 0.95 if plan.auto_captions else 0.5
        plan.reasoning["captions"] = f"{plan.caption_style} captions: {plan.auto_captions and 'enabled' or 'disabled'}"

        # 3. Silence removal - content-aware
        plan.remove_silence, plan.silence_aggressiveness, plan.preserve_pauses = self._decide_silence_removal(
            moment, moment_score, story
        )
        plan.reasoning["silence"] = f"Silence removal: {plan.silence_aggressiveness}"

        # 4. Background blur - for gameplay/desktop content
        plan.blur = self._decide_blur(moment, transcript)
        plan.reasoning["blur"] = f"Background blur: {'enabled' if plan.blur else 'disabled'}"

        # 5. Enhancement - based on visual quality needs
        plan.enhance = self._decide_enhance(moment, moment_score)
        plan.reasoning["enhance"] = f"Enhancement: {plan.enhance}"

        # 6. Zoom - for face-focused or detail content
        plan.zoom = self._decide_zoom(moment, transcript)
        plan.reasoning["zoom"] = f"Zoom: {'enabled' if plan.zoom else 'disabled'}"

        # 7. Mirror - for avoiding copyright (use sparingly)
        plan.mirror = self._decide_mirror(moment, transcript)
        plan.reasoning["mirror"] = f"Mirror: {'enabled' if plan.mirror else 'disabled'}"

        # 8. Overlay - streamer branding
        plan.overlay = True  # Usually enabled for attribution
        plan.reasoning["overlay"] = "Streamer overlay for attribution"

        # 9. Split screen - only for specific content types
        plan.split_screen = self._decide_split_screen(moment, transcript)
        plan.split_screen_confidence = 0.8 if not plan.split_screen else 0.3
        plan.reasoning["split_screen"] = f"Split screen: {'enabled' if plan.split_screen else 'disabled'}"

        # 10. Custom crop for face-focused content
        plan.custom_crop = self._decide_custom_crop(moment, transcript)

        # 11. Clip structure
        plan.hook_start = moment.expanded_start
        plan.hook_description = moment.hook_text[:100]
        plan.context_required = True
        plan.context_description = moment.context_text[:100]
        plan.payoff_timestamp = moment.original_end
        plan.payoff_description = moment.payoff_text[:100]
        plan.ending_timestamp = moment.expanded_end
        plan.ending_description = moment.reaction_text[:100]

        # 12. Editing intensity
        plan.editing_intensity = self._decide_editing_intensity(moment, moment_score, story)
        plan.reasoning["intensity"] = f"Editing intensity: {plan.editing_intensity}"

        # 13. Audio processing
        plan.audio_processing = self._decide_audio(moment, moment_score)
        plan.reasoning["audio"] = f"Audio processing: {plan.audio_processing}"

        # Validate and adjust
        self._validate_plan(plan, moment)

        return plan

    def _detect_clip_type(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis,
        moment_score: MomentScore
    ) -> str:
        """Detect the type of clip based on content analysis."""
        text = (moment.hook_text + " " + moment.payoff_text + " " + moment.reaction_text).lower()

        # Check for specific patterns
        if any(w in text for w in ["lol", "lmao", "haha", "funny", "joke"]):
            return "funny_moment"
        if any(w in text for w in ["react", "reaction", "my face", "watch this"]):
            return "reaction"
        if any(w in text for w in ["you're wrong", "actually no", "debate", "argument"]):
            return "argument"
        if any(w in text for w in ["cry", "crying", "heartbroken", "tears", "emotional"]):
            return "emotional"
        if any(w in text for w in ["controversy", "hot take", "unpopular", "wrong"]):
            return "controversial"
        if any(w in text for w in ["story time", "so i was", "this happened", "let me tell"]):
            return "story"
        if any(w in text for w in ["game", "playing", "match", "kill", "win", "lose", "fps"]):
            return "gameplay"
        if any(w in text for w in ["surprise", "wait what", "no way", "omg", "unbelievable"]):
            return "surprise"
        if any(w in text for w in ["fail", "mistake", "oops", "wrong"]):
            return "fail"
        if any(w in text for w in ["win", "victory", "clutch", "first", "ranked"]):
            return "win"
        if any(w in text for w in ["announce", "announcement", "new", "update"]):
            return "announcement"
        if any(w in text for w in ["interview", "question", "asked"]):
            return "interview"
        if any(w in text for w in ["chat", "guys", "listen", "look"]):
            return "conversation"
        return "other"

    def _decide_editing_intensity(
        self,
        moment: ExpandedMoment,
        moment_score: MomentScore,
        story: StoryAnalysis
    ) -> str:
        """Decide editing intensity based on content."""
        if story.has_complete_arc and story.hook.score > 60 and story.payoff.score > 60:
            return "high_energy"
        if moment_score.emotion_score > 60 or moment_score.humor_score > 60:
            return "moderate"
        return "natural"

    def _decide_audio(
        self,
        moment: ExpandedMoment,
        moment_score: MomentScore
    ) -> str:
        """Decide audio processing needs."""
        if moment_score.emotion_score > 70:
            return "normalize"
        if moment_score.audio_energy > 20:
            return "enhance"
        return "none"

    def _decide_captions(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis
    ) -> Tuple[bool, str]:
        """Decide caption strategy."""
        # Get speech segments in clip
        segments = [
            s for s in transcript.segments
            if s.end > moment.expanded_start and s.start < moment.expanded_end
        ]

        if not segments:
            return False, "none"

        word_count = sum(len(s.words) for s in segments)
        duration = moment.duration

        # Always caption if significant speech
        if word_count < 10:
            return False, "none"

        # Caption style based on duration and density
        if duration <= 20 and word_count > 30:
            # Fast, dense speech - word-by-word
            return True, "word_by_word"
        elif duration <= 40:
            # Medium - word-by-word
            return True, "word_by_word"
        else:
            # Long - sentence by sentence
            return True, "sentence_by_sentence"

    def _decide_silence_removal(
        self,
        moment: ExpandedMoment,
        moment_score: MomentScore,
        story: StoryAnalysis
    ) -> Tuple[bool, str, List[Tuple[float, float]]]:
        """Decide silence removal strategy."""
        # Check for meaningful pauses that should be preserved
        preserve = list(moment.dead_space_removed)  # These are already identified as removable

        # But also check story analysis for pauses to preserve
        for rec in story.recommendations:
            if "pause" in rec.lower() or "comedic timing" in rec.lower():
                # Find pauses near payoff/reaction
                for pause_start, pause_end in moment.dead_space_removed:
                    if abs(pause_start - moment.original_end) < 3.0:
                        # This pause is near the payoff - might be comedic
                        if (pause_start, pause_end) not in preserve:
                            preserve.append((pause_start, pause_end))

        # Decide aggressiveness based on content
        if moment_score.pacing_score < 40:
            # Slow pacing - aggressive removal
            return True, "aggressive", preserve
        elif moment.duration > 60:
            # Long clip - moderate removal
            return True, "moderate", preserve
        elif moment_score.humor_score > 60 or moment_score.surprise_score > 60:
            # Comedy/surprise - light removal to preserve timing
            return True, "light", preserve
        else:
            return True, "moderate", preserve

    def _decide_blur(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis
    ) -> bool:
        """Decide if background blur is appropriate."""
        # Blur is good for:
        # - Gameplay footage (desktop capture)
        # - When streamer face is small
        # - Busy backgrounds that distract

        text = (moment.hook_text + moment.context_text + moment.payoff_text).lower()

        # Keywords suggesting gameplay/desktop
        gameplay_keywords = [
            "game", "playing", "match", "round", "kill", "win", "lose",
            "settings", "menu", "inventory", "map", "spawn", "loot",
            "fps", "aim", "shot", "headshot", "clutch", "ranked"
        ]

        # Keywords suggesting just talking/cam
        talking_keywords = [
            "chat", "guys", "story", "happened", "said", "told",
            "thinking", "feel", "think", "opinion", "react"
        ]

        gameplay_score = sum(1 for kw in gameplay_keywords if kw in text)
        talking_score = sum(1 for kw in talking_keywords if kw in text)

        # If more gameplay indicators, blur helps focus on streamer
        return gameplay_score > talking_score

    def _decide_enhance(
        self,
        moment: ExpandedMoment,
        moment_score: MomentScore
    ) -> str:
        """Decide enhancement level."""
        # Subtle for most content
        # Vivid for high-energy, gaming, reaction content

        if moment_score.emotion_score > 70 or moment_score.humor_score > 70:
            return "Vivid"
        elif moment_score.surprise_score > 60:
            return "Vivid"
        elif moment.duration < 15:
            return "Subtle"
        else:
            return "Subtle"

    def _decide_zoom(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis
    ) -> bool:
        """Decide if zoom is appropriate."""
        # Zoom helps for:
        # - Face cam content (focus on reaction)
        # - Small text/UI elements
        # - Detail-focused moments

        text = (moment.hook_text + moment.payoff_text + moment.reaction_text).lower()

        face_keywords = [
            "face", "react", "reaction", "expression", "look at",
            "my face", "camera", "cam", "webcam"
        ]

        detail_keywords = [
            "text", "chat", "message", "donation", "sub", "alert",
            "number", "stat", "score", "timer", "health", "ammo"
        ]

        face_score = sum(1 for kw in face_keywords if kw in text)
        detail_score = sum(1 for kw in detail_keywords if kw in text)

        return face_score > 0 or detail_score > 0

    def _decide_mirror(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis
    ) -> bool:
        """Decide if mirror is appropriate."""
        # Mirror is primarily for copyright avoidance
        # Use sparingly and only when necessary
        # For now, default to False - let user decide
        return False

    def _decide_split_screen(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis
    ) -> bool:
        """Decide if split screen is appropriate."""
        # Split screen for:
        # - Reaction to external content (video, tweet, image)
        # - Before/after comparisons
        # - Side-by-side gameplay vs facecam

        text = (moment.hook_text + moment.context_text).lower()

        split_keywords = [
            "video", "tweet", "post", "image", "picture", "screenshot",
            "look at this", "check this", "see this", "watch this",
            "react to", "responding to", "reply to"
        ]

        return any(kw in text for kw in split_keywords)

    def _decide_custom_crop(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis
    ) -> Optional[str]:
        """Decide custom crop for face-focused content."""
        # This would need face detection to be precise
        # For now, return None - use default center crop
        return None

    def _validate_plan(self, plan: EditPlan, moment: ExpandedMoment):
        """Validate and adjust plan for consistency."""
        # Don't use aggressive silence removal with word-by-word captions on long clips
        if (plan.silence_aggressiveness == "aggressive" and
            plan.caption_style == "word_by_word" and
            moment.duration > 45):
            plan.silence_aggressiveness = "moderate"
            plan.warnings.append("Reduced silence aggressiveness for long captioned clip")

        # Don't blur if split screen (split screen handles background)
        if plan.split_screen and plan.blur:
            plan.blur = False
            plan.warnings.append("Disabled blur for split screen")

        # Ensure captions enabled if auto_captions in preset
        if self.config.enable_smart_captions and not plan.auto_captions:
            # Only auto-enable if there's significant speech
            if moment.moment_score and moment.moment_score.transcript_score > 20:
                plan.auto_captions = True
                plan.caption_style = "word_by_word" if moment.duration < 30 else "sentence_by_sentence"

    def apply_preset_overrides(self, plan: EditPlan, preset_name: str):
        """Apply preset overrides (like TikTok preset) while respecting content-aware decisions."""
        from .smart_presets import get_preset

        preset = get_preset(preset_name)
        if not preset:
            return

        # Apply preset but keep content-aware decisions for key options
        content_aware_keys = {"auto_captions", "remove_silence", "blur", "zoom", "enhance"}

        for key, value in preset.items():
            if key not in content_aware_keys:
                setattr(plan, key, value)


def generate_ffmpeg_filter_plan(edit_plan: EditPlan, moment: ExpandedMoment) -> Dict[str, Any]:
    """
    Generate FFmpeg filter parameters from edit plan.
    This bridges the gap between high-level plan and main.py's edit_video function.
    """
    return {
        "size": edit_plan.size,
        "zoom": edit_plan.zoom,
        "mirror": edit_plan.mirror,
        "blur": edit_plan.blur,
        "enhance": edit_plan.enhance,
        "split_screen": edit_plan.split_screen,
        # Additional params for advanced features
        "caption_style": edit_plan.caption_style if edit_plan.auto_captions else "none",
        "silence_aggressiveness": edit_plan.silence_aggressiveness,
        "preserve_pauses": edit_plan.preserve_pauses,
        "custom_crop": edit_plan.custom_crop,
        "color_grade": edit_plan.color_grade,
    }