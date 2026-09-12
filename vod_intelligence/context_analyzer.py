"""
Context Analyzer - Expands detected moments with before/after context.

Determines:
- Minimum necessary context for understanding
- Meaningful pauses vs dead space
- Hook→Context→Payoff→Reaction structure
- Optimal clip boundaries
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
from pathlib import Path

from .config import CONFIG, ContextConfig
from .transcript_analyzer import TranscriptAnalysis, TranscriptSegment
from .audio_analyzer import AudioAnalyzer, AudioSignal
from .chat_analyzer import ChatSignal


@dataclass
class ContextWindow:
    """Context around a moment."""
    moment_start: float
    moment_end: float
    pre_context_start: float
    pre_context_end: float
    post_context_start: float
    post_context_end: float
    total_duration: float
    context_quality: float  # 0-100
    necessary_pre_seconds: float
    necessary_post_seconds: float
    reasoning: str


@dataclass
class ExpandedMoment:
    """A moment expanded with intelligent context."""
    original_start: float
    original_end: float
    expanded_start: float
    expanded_end: float
    duration: float
    context: ContextWindow
    hook_text: str
    context_text: str
    payoff_text: str
    reaction_text: str
    story_complete: bool
    dead_space_removed: List[Tuple[float, float]]
    edit_recommendations: Dict[str, Any]


class ContextAnalyzer:
    """
    Analyzes and expands moments with intelligent context.
    """

    def __init__(self, config: ContextConfig = None):
        self.config = config or CONFIG.context

    def expand_moment(
        self,
        moment_start: float,
        moment_end: float,
        transcript: TranscriptAnalysis,
        audio_signals: List[AudioSignal],
        chat_signals: List[ChatSignal],
        duration: float
    ) -> ExpandedMoment:
        """
        Expand a moment with intelligent before/after context.

        The goal: MAXIMUM UNDERSTANDING WITH MINIMUM DEAD SPACE
        """
        # Find relevant transcript segments
        moment_segments = self._get_segments_in_range(transcript, moment_start, moment_end)

        # Determine hook (what grabs attention)
        hook_text, hook_start = self._identify_hook(moment_segments, moment_start)

        # Determine payoff (satisfying conclusion)
        payoff_text, payoff_end = self._identify_payoff(moment_segments, moment_end)

        # Determine reaction (streamer/chat reaction)
        reaction_text, reaction_end = self._identify_reaction(
            moment_segments, moment_end, audio_signals, chat_signals
        )

        # Calculate necessary pre-context
        pre_needed = self._calculate_pre_context(
            transcript, moment_start, hook_start, audio_signals
        )

        # Calculate necessary post-context
        post_needed = self._calculate_post_context(
            transcript, moment_end, payoff_end, reaction_end, audio_signals
        )

        # Apply limits
        pre_needed = min(pre_needed, self.config.pre_context_seconds)
        post_needed = min(post_needed, self.config.post_context_seconds)

        expanded_start = max(0, moment_start - pre_needed)
        expanded_end = min(duration, moment_end + post_needed)

        # Ensure minimum duration
        if expanded_end - expanded_start < self.config.min_context_duration:
            # Extend symmetrically
            center = (expanded_start + expanded_end) / 2
            half_min = self.config.min_context_duration / 2
            expanded_start = max(0, center - half_min)
            expanded_end = min(duration, center + half_min)

        # Ensure maximum duration
        if expanded_end - expanded_start > self.config.max_total_duration:
            # Trim from the less important side
            excess = (expanded_end - expanded_start) - self.config.max_total_duration
            if pre_needed > post_needed:
                expanded_start += min(excess, pre_needed)
            else:
                expanded_end -= min(excess, post_needed)

        # Identify dead space to remove
        dead_space = self._identify_dead_space(
            transcript, audio_signals, expanded_start, expanded_end
        )

        # Build context window
        context = ContextWindow(
            moment_start=moment_start,
            moment_end=moment_end,
            pre_context_start=expanded_start,
            pre_context_end=moment_start,
            post_context_start=moment_end,
            post_context_end=expanded_end,
            total_duration=expanded_end - expanded_start,
            context_quality=self._score_context_quality(
                transcript, expanded_start, expanded_end, moment_start, moment_end
            ),
            necessary_pre_seconds=pre_needed,
            necessary_post_seconds=post_needed,
            reasoning=self._build_reasoning(pre_needed, post_needed, hook_text, payoff_text)
        )

        # Get context text
        context_segments = self._get_segments_in_range(transcript, expanded_start, moment_start)
        context_text = " ".join(s.text for s in context_segments)

        return ExpandedMoment(
            original_start=moment_start,
            original_end=moment_end,
            expanded_start=expanded_start,
            expanded_end=expanded_end,
            duration=expanded_end - expanded_start,
            context=context,
            hook_text=hook_text,
            context_text=context_text,
            payoff_text=payoff_text,
            reaction_text=reaction_text,
            story_complete=self._is_story_complete(hook_text, payoff_text, reaction_text),
            dead_space_removed=dead_space,
            edit_recommendations=self._generate_edit_recommendations(
                transcript, audio_signals, dead_space, expanded_start, expanded_end
            )
        )

    def _get_segments_in_range(
        self,
        transcript: TranscriptAnalysis,
        start: float,
        end: float
    ) -> List[TranscriptSegment]:
        """Get transcript segments overlapping with time range."""
        return [
            s for s in transcript.segments
            if s.end > start and s.start < end
        ]

    def _identify_hook(
        self,
        segments: List[TranscriptSegment],
        moment_start: float
    ) -> Tuple[str, float]:
        """Identify the hook - the attention-grabbing opening."""
        if not segments:
            return "", moment_start

        # Look for hook candidates in the first few segments
        for seg in segments[:5]:
            text = seg.text.strip()
            if self._is_strong_hook(text):
                return text, seg.start

        # Fallback: first meaningful segment
        for seg in segments:
            if len(seg.text.strip()) > 10:
                return seg.text.strip(), seg.start

        return segments[0].text.strip() if segments else "", moment_start

    def _is_strong_hook(self, text: str) -> bool:
        """Check if text is a strong hook."""
        text_lower = text.lower()
        hook_indicators = [
            # Questions
            "what", "why", "how", "wait", "bro", "no way",
            # Reactions
            "omg", "wtf", "holy", "insane", "crazy", "unbelievable",
            # Statements
            "i can't believe", "this is", "look at", "watch this",
            # Controversy
            "you're wrong", "actually", "the truth", "nobody knows",
        ]
        return any(ind in text_lower for ind in hook_indicators)

    def _identify_payoff(
        self,
        segments: List[TranscriptSegment],
        moment_end: float
    ) -> Tuple[str, float]:
        """Identify the payoff - satisfying conclusion."""
        if not segments:
            return "", moment_end

        # Look at last segments for payoff
        for seg in reversed(segments[-5:]):
            text = seg.text.strip()
            if self._is_payoff(text):
                return text, seg.end

        # Fallback: last meaningful segment
        for seg in reversed(segments):
            if len(seg.text.strip()) > 10:
                return seg.text.strip(), seg.end

        return segments[-1].text.strip() if segments else "", moment_end

    def _is_payoff(self, text: str) -> bool:
        """Check if text is a payoff."""
        text_lower = text.lower()
        payoff_indicators = [
            # Resolutions
            "that's why", "so that's", "turns out", "ended up",
            # Reactions
            "lmao", "lol", "haha", "that was", "omg that",
            # Conclusions
            "and then", "finally", "in the end", "result",
            # Punchlines
            "got em", "rekt", "owned", "destroyed",
        ]
        return any(ind in text_lower for ind in payoff_indicators)

    def _identify_reaction(
        self,
        segments: List[TranscriptSegment],
        moment_end: float,
        audio_signals: List[AudioSignal],
        chat_signals: List[ChatSignal]
    ) -> Tuple[str, float]:
        """Identify reaction after the payoff."""
        # Check for audio signals (laughter, energy shift) after moment
        reaction_signals = [
            s for s in audio_signals + chat_signals
            if s.timestamp > moment_end and s.timestamp < moment_end + 10
        ]

        if reaction_signals:
            # Find corresponding transcript
            for seg in segments:
                if seg.start >= moment_end and seg.start < moment_end + 10:
                    return seg.text.strip(), seg.end

        return "", moment_end

    def _calculate_pre_context(
        self,
        transcript: TranscriptAnalysis,
        moment_start: float,
        hook_start: float,
        audio_signals: List[AudioSignal]
    ) -> float:
        """Calculate how much pre-context is needed."""
        # If hook is at moment start, need context before it
        if hook_start <= moment_start + 1.0:
            # Look for topic setup in previous 10 seconds
            prev_segments = [
                s for s in transcript.segments
                if s.end <= moment_start and s.start >= moment_start - 15
            ]

            # Find the last topic shift or scene change
            for seg in reversed(prev_segments):
                if self._is_topic_start(seg.text):
                    return moment_start - seg.start

            # Check audio for scene change
            audio_scene = [
                s for s in audio_signals
                if s.signal_type in ("energy_shift_up", "energy_shift_down")
                and moment_start - 10 <= s.timestamp <= moment_start
            ]
            if audio_scene:
                return moment_start - audio_scene[-1].timestamp

        return self.config.pre_context_seconds

    def _calculate_post_context(
        self,
        transcript: TranscriptAnalysis,
        moment_end: float,
        payoff_end: float,
        reaction_end: float,
        audio_signals: List[AudioSignal]
    ) -> float:
        """Calculate how much post-context is needed."""
        # Need to capture reaction and resolution
        latest_important = max(payoff_end, reaction_end, moment_end)

        # Look for natural ending point
        next_segments = [
            s for s in transcript.segments
            if s.start >= latest_important and s.start <= latest_important + 15
        ]

        for seg in next_segments:
            if self._is_natural_end(seg.text):
                return seg.end - moment_end

        # Check audio for energy drop (natural end)
        audio_drops = [
            s for s in audio_signals
            if s.signal_type == "energy_shift_down"
            and latest_important <= s.timestamp <= latest_important + 10
        ]
        if audio_drops:
            return audio_drops[0].timestamp - moment_end

        return self.config.post_context_seconds

    def _is_topic_start(self, text: str) -> bool:
        """Check if text indicates a new topic."""
        text_lower = text.lower()
        starters = [
            "so ", "anyway", "by the way", "speaking of", "oh yeah",
            "remember when", "did you see", "have you heard", "new topic"
        ]
        return any(text_lower.startswith(s) for s in starters)

    def _is_natural_end(self, text: str) -> bool:
        """Check if text is a natural conversation end."""
        text_lower = text.lower().strip()
        enders = [
            "anyway", "so yeah", "that's it", "moving on", "next topic",
            "okay so", "alright", "cool", "nice", "gg", "good game"
        ]
        return any(e in text_lower for e in enders)

    def _identify_dead_space(
        self,
        transcript: TranscriptAnalysis,
        audio_signals: List[AudioSignal],
        start: float,
        end: float
    ) -> List[Tuple[float, float]]:
        """Identify removable dead space within the expanded clip."""
        dead_space = []

        # Get silence segments from audio
        silence_segments = self._get_silence_segments(audio_signals, start, end)

        for silence_start, silence_end in silence_segments:
            duration = silence_end - silence_start

            # Skip if it's a meaningful pause
            if self._is_meaningful_pause(silence_start, silence_end, transcript, audio_signals):
                continue

            # Skip very short pauses (natural speech rhythm)
            if duration < 0.5:
                continue

            # Skip if it's at the very beginning or end (padding)
            if silence_start - start < 0.5 or end - silence_end < 0.5:
                continue

            dead_space.append((silence_start, silence_end))

        return dead_space

    def _get_silence_segments(
        self,
        audio_signals: List[AudioSignal],
        start: float,
        end: float
    ) -> List[Tuple[float, float]]:
        """Extract silence segments from audio signals."""
        # This would ideally come from the audio analyzer's silence detection
        # For now, return empty - the audio_analyzer.get_silence_segments() should be used
        return []

    def _is_meaningful_pause(
        self,
        start: float,
        end: float,
        transcript: TranscriptAnalysis,
        audio_signals: List[AudioSignal]
    ) -> bool:
        """Determine if a pause is meaningful (comedic timing, suspense, etc.)."""
        duration = end - start

        # Check for meaningful_pause audio signal
        pause_signals = [
            s for s in audio_signals
            if s.signal_type == "meaningful_pause"
            and abs(s.timestamp - (start + end) / 2) < 1.0
        ]
        if pause_signals:
            return True

        # Check transcript context - pause between question and answer
        before_seg = None
        after_seg = None
        for seg in transcript.segments:
            if seg.end <= start and (before_seg is None or seg.end > before_seg.end):
                before_seg = seg
            if seg.start >= end and (after_seg is None or seg.start < after_seg.start):
                after_seg = seg

        if before_seg and after_seg:
            # Question followed by answer = meaningful
            if '?' in before_seg.text and len(after_seg.text) > 5:
                return True
            # Setup followed by punchline
            if self._is_strong_hook(before_seg.text) and self._is_payoff(after_seg.text):
                return True

        # Long pauses (>3s) in the middle of speech are usually dead space
        # unless they're clearly dramatic
        if duration > 3.0:
            # Check for dramatic audio cues
            dramatic = [
                s for s in audio_signals
                if s.signal_type in ("energy_shift_down", "laughter")
                and abs(s.timestamp - start) < 2.0
            ]
            if not dramatic:
                return False  # Likely dead space

        return duration > self.config.pause_significance_threshold

    def _score_context_quality(
        self,
        transcript: TranscriptAnalysis,
        expanded_start: float,
        expanded_end: float,
        moment_start: float,
        moment_end: float
    ) -> float:
        """Score the quality of context (0-100)."""
        segments = self._get_segments_in_range(transcript, expanded_start, expanded_end)
        if not segments:
            return 0.0

        # Factors:
        # 1. Hook present in pre-context
        pre_segments = [s for s in segments if s.end <= moment_start]
        has_hook = any(self._is_strong_hook(s.text) for s in pre_segments)

        # 2. Payoff present in post-context
        post_segments = [s for s in segments if s.start >= moment_end]
        has_payoff = any(self._is_payoff(s.text) for s in post_segments)

        # 3. Coherent narrative (entities/topics consistent)
        topic_consistency = 1.0  # Simplified

        # 4. Duration appropriateness
        duration = expanded_end - expanded_start
        duration_score = 1.0
        if duration < 10:
            duration_score = 0.5
        elif duration > 60:
            duration_score = 0.7

        score = 0.0
        if has_hook:
            score += 35
        if has_payoff:
            score += 35
        score += topic_consistency * 15
        score += duration_score * 15

        return min(100.0, score)

    def _is_story_complete(self, hook: str, payoff: str, reaction: str) -> bool:
        """Check if the story has hook, payoff, and reaction."""
        return bool(hook and payoff and (reaction or len(payoff) > 20))

    def _build_reasoning(self, pre: float, post: float, hook: str, payoff: str) -> str:
        """Build human-readable reasoning for context decisions."""
        parts = []
        if pre > 3:
            parts.append(f"{pre:.1f}s pre-context for setup: '{hook[:50]}...'")
        else:
            parts.append(f"Minimal pre-context ({pre:.1f}s) - hook is immediate")
        if post > 3:
            parts.append(f"{post:.1f}s post-context for payoff: '{payoff[:50]}...'")
        else:
            parts.append(f"Short post-context ({post:.1f}s) - payoff is self-contained")
        return "; ".join(parts)

    def _generate_edit_recommendations(
        self,
        transcript: TranscriptAnalysis,
        audio_signals: List[AudioSignal],
        dead_space: List[Tuple[float, float]],
        start: float,
        end: float
    ) -> Dict[str, Any]:
        """Generate content-aware edit recommendations."""
        duration = end - start
        segments = self._get_segments_in_range(transcript, start, end)
        word_count = sum(len(s.words) for s in segments)

        return {
            "remove_dead_space": len(dead_space) > 0,
            "dead_space_segments": dead_space,
            "add_captions": word_count > 20 and duration > 10,
            "caption_style": "word_by_word" if duration < 30 else "sentence_by_sentence",
            "use_blur_background": duration > 15,
            "enhance_level": "Subtle" if duration > 10 else "Off",
            "zoom": duration < 20,
            "mirror": False,
            "split_screen": False,
            "preserve_pauses": [s for s in dead_space if end - s[1] < 2.0],  # Don't cut pauses near end
        }


def merge_overlapping_moments(
    moments: List[ExpandedMoment],
    threshold: float = 5.0
) -> List[ExpandedMoment]:
    """Merge moments that overlap or are very close."""
    if not moments:
        return []

    sorted_moments = sorted(moments, key=lambda m: m.expanded_start)
    merged = [sorted_moments[0]]

    for moment in sorted_moments[1:]:
        last = merged[-1]
        if moment.expanded_start <= last.expanded_end + threshold:
            # Merge
            last.expanded_end = max(last.expanded_end, moment.expanded_end)
            last.duration = last.expanded_end - last.expanded_start
            last.context.moment_end = moment.context.moment_end
            last.context.total_duration = last.duration
            # Combine context texts
            last.context_text += " " + moment.context_text
            last.payoff_text = moment.payoff_text or last.payoff_text
            last.reaction_text = moment.reaction_text or last.reaction_text
            last.dead_space_removed.extend(moment.dead_space_removed)
        else:
            merged.append(moment)

    return merged