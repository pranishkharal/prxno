"""
Story Analyzer - Evaluates hook/context/payoff/reaction structure of clips.

Analyzes whether a clip has:
1. HOOK - Grabs attention in first 3 seconds
2. CONTEXT - Sets up the situation
3. PAYOFF - Delivers satisfaction/conclusion
4. REACTION - Emotional response

Scores each component and overall story quality.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path

from .config import CONFIG, StoryConfig
from .transcript_analyzer import TranscriptAnalysis, TranscriptSegment
from .context_analyzer import ExpandedMoment
from .audio_analyzer import AudioSignal
from .chat_analyzer import ChatSignal


@dataclass
class StoryComponent:
    """Score for one story component."""
    name: str  # "hook", "context", "payoff", "reaction"
    score: float  # 0-100
    evidence: str
    timestamp: float
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StoryAnalysis:
    """Complete story structure analysis."""
    hook: StoryComponent
    context: StoryComponent
    payoff: StoryComponent
    reaction: StoryComponent
    overall_score: float
    has_complete_arc: bool
    pacing_rating: str  # "rushed", "good", "slow"
    engagement_curve: List[Tuple[float, float]]  # (time, engagement)
    recommendations: List[str]


class StoryAnalyzer:
    """
    Analyzes story structure of expanded moments.
    """

    def __init__(self, config: StoryConfig = None):
        self.config = config or CONFIG.story

    def analyze(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis,
        audio_signals: List[AudioSignal],
        chat_signals: List[ChatSignal]
    ) -> StoryAnalysis:
        """
        Analyze the complete story structure.

        Returns StoryAnalysis with component scores and overall rating.
        """
        # Analyze each component
        hook = self._analyze_hook(moment, transcript, audio_signals, chat_signals)
        context = self._analyze_context(moment, transcript, audio_signals)
        payoff = self._analyze_payoff(moment, transcript, audio_signals, chat_signals)
        reaction = self._analyze_reaction(moment, transcript, audio_signals, chat_signals)

        # Calculate overall score
        overall = (
            hook.score * self.config.hook_weight +
            context.score * self.config.context_weight +
            payoff.score * self.config.payoff_weight +
            reaction.score * self.config.reaction_weight
        )

        # Check if story arc is complete
        has_arc = (
            hook.score >= self.config.min_hook_score and
            payoff.score >= self.config.min_payoff_score and
            context.score > 30
        )

        # Pacing analysis
        pacing = self._analyze_pacing(moment, transcript)

        # Engagement curve
        engagement = self._build_engagement_curve(moment, transcript, audio_signals, chat_signals)

        # Recommendations
        recommendations = self._generate_recommendations(hook, context, payoff, reaction, pacing)

        return StoryAnalysis(
            hook=hook,
            context=context,
            payoff=payoff,
            reaction=reaction,
            overall_score=overall,
            has_complete_arc=has_arc,
            pacing_rating=pacing,
            engagement_curve=engagement,
            recommendations=recommendations,
        )

    def _analyze_hook(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis,
        audio_signals: List[AudioSignal],
        chat_signals: List[ChatSignal]
    ) -> StoryComponent:
        """Analyze the hook (first 3 seconds of expanded clip)."""
        hook_end = moment.expanded_start + 3.0

        # Get transcript in hook window
        hook_segments = [
            s for s in transcript.segments
            if s.start >= moment.expanded_start and s.start < hook_end
        ]

        hook_text = " ".join(s.text for s in hook_segments)
        score = 0.0
        evidence = ""

        # 1. Transcript-based hook detection
        if hook_text:
            score, evidence = self._score_hook_text(hook_text, moment.expanded_start)

        # 2. Audio energy spike at start
        audio_hooks = [
            s for s in audio_signals
            if s.signal_type in ("energy_spike", "energy_shift_up")
            and abs(s.timestamp - moment.expanded_start) < 2.0
        ]
        if audio_hooks:
            audio_boost = min(30, max(s.strength for s in audio_hooks) * 0.5)
            score = min(100, score + audio_boost)
            evidence += f" | Audio spike: {max(s.strength for s in audio_hooks):.0f}"

        # 3. Chat spike at start
        chat_hooks = [
            s for s in chat_signals
            if s.signal_type in ("spike", "emote_burst", "reaction_excitement")
            and abs(s.timestamp - moment.expanded_start) < 3.0
        ]
        if chat_hooks:
            chat_boost = min(20, max(s.strength for s in chat_hooks) * 0.3)
            score = min(100, score + chat_boost)
            evidence += f" | Chat reaction: {max(s.strength for s in chat_hooks):.0f}"

        # 4. LLM-identified hooks
        llm_hooks = [
            h for h in transcript.hook_candidates
            if abs(h.get("timestamp", 0) - moment.expanded_start) < 3.0
        ]
        if llm_hooks:
            llm_boost = max(h.get("strength", 0) for h in llm_hooks) * 0.4
            score = min(100, score + llm_boost)
            evidence += f" | LLM hook: {llm_hooks[0].get('hook_type', 'unknown')}"

        return StoryComponent(
            name="hook",
            score=score,
            evidence=evidence.strip(" |"),
            timestamp=moment.expanded_start,
            details={"hook_text": hook_text[:100], "llm_hooks": len(llm_hooks)}
        )

    def _score_hook_text(self, text: str, timestamp: float) -> Tuple[float, str]:
        """Score hook quality from text."""
        text_lower = text.lower()
        score = 0.0
        reasons = []

        # Strong hook patterns
        patterns = {
            # Questions (curiosity)
            "question": (["what", "why", "how", "wait", "bro", "no way", "really?"], 25, "Question hook"),
            # Exclamations (shock/surprise)
            "exclamation": (["omg", "wtf", "holy", "insane", "crazy", "unbelievable", "no way"], 30, "Exclamation hook"),
            # Direct address
            "address": (["chat", "guys", "listen", "look", "watch"], 20, "Direct address"),
            # Controversial/strong statements
            "controversial": (["you're wrong", "actually", "the truth", "nobody knows", "secret"], 25, "Controversial statement"),
            # Story starters
            "story": (["so i was", "story time", "this happened", "let me tell you"], 20, "Story starter"),
            # Reaction starters
            "reaction": (["my reaction", "i reacted", "my face", "i couldn't"], 20, "Reaction setup"),
        }

        for pattern_name, (keywords, points, reason) in patterns.items():
            if any(kw in text_lower for kw in keywords):
                score += points
                reasons.append(reason)

        # Length penalty for very short hooks
        if len(text.strip()) < 10:
            score *= 0.5

        # Cap at 100
        score = min(100, score)

        evidence = "; ".join(reasons) if reasons else "Weak hook"
        return score, evidence

    def _analyze_context(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis,
        audio_signals: List[AudioSignal]
    ) -> StoryComponent:
        """Analyze context (setup before the main moment)."""
        context_end = moment.original_start
        context_start = moment.expanded_start

        if context_end <= context_start:
            return StoryComponent(
                name="context",
                score=0,
                evidence="No pre-context",
                timestamp=context_start,
            )

        context_segments = [
            s for s in transcript.segments
            if s.end > context_start and s.start < context_end
        ]

        context_text = " ".join(s.text for s in context_segments)
        score = 0.0
        reasons = []

        if context_text:
            # Context provides setup information
            setup_indicators = [
                "so ", "because ", "since ", "when ", "after ", "before ",
                "i was ", "we were ", "he was ", "she was ", "they were ",
                "playing ", "doing ", "trying ", "happened ", "saw "
            ]
            if any(ind in context_text.lower() for ind in setup_indicators):
                score += 30
                reasons.append("Narrative setup present")

            # Entity introduction (names, games, topics)
            if moment.context_text:
                score += 20
                reasons.append("Entities/topics introduced")

            # Duration appropriateness
            context_duration = context_end - context_start
            if 2 <= context_duration <= 10:
                score += 25
                reasons.append(f"Good context duration ({context_duration:.1f}s)")
            elif context_duration > 10:
                score += 10
                reasons.append("Long context")
            else:
                score += 5
                reasons.append("Brief context")

        # Audio context (scene setting)
        audio_context = [
            s for s in audio_signals
            if s.signal_type in ("energy_shift_up", "speech_burst")
            and context_start <= s.timestamp < context_end
        ]
        if audio_context:
            score += 15
            reasons.append("Audio scene setting")

        # Topic consistency
        if transcript.topics:
            score += 10
            reasons.append("Clear topic")

        return StoryComponent(
            name="context",
            score=min(100, score),
            evidence="; ".join(reasons) if reasons else "Insufficient context",
            timestamp=context_start,
            details={"context_text": context_text[:100], "duration": context_end - context_start}
        )

    def _analyze_payoff(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis,
        audio_signals: List[AudioSignal],
        chat_signals: List[ChatSignal]
    ) -> StoryComponent:
        """Analyze payoff (satisfying conclusion)."""
        payoff_start = moment.original_end
        payoff_end = min(moment.expanded_end, moment.original_end + 10)

        payoff_segments = [
            s for s in transcript.segments
            if s.start >= payoff_start and s.start < payoff_end
        ]

        payoff_text = " ".join(s.text for s in payoff_segments)
        score = 0.0
        reasons = []

        if payoff_text:
            score, reasons = self._score_payoff_text(payoff_text, payoff_start)

        # LLM-identified payoffs
        llm_payoffs = [
            p for p in transcript.payoff_candidates
            if abs(p.get("timestamp", 0) - moment.original_end) < 5.0
        ]
        if llm_payoffs:
            llm_boost = max(p.get("satisfaction", 0) for p in llm_payoffs) * 0.5
            score = min(100, score + llm_boost)
            reasons.append(f"LLM payoff: {llm_payoffs[0].get('payoff_type', 'unknown')}")

        # Audio payoff (laughter, energy drop after climax)
        audio_payoffs = [
            s for s in audio_signals
            if s.signal_type in ("laughter", "energy_shift_down")
            and payoff_start <= s.timestamp < payoff_end
        ]
        if audio_payoffs:
            audio_boost = min(25, max(s.strength for s in audio_payoffs) * 0.4)
            score = min(100, score + audio_boost)
            reasons.append(f"Audio payoff: {audio_payoffs[0].signal_type}")

        # Chat payoff (emote burst after moment)
        chat_payoffs = [
            s for s in chat_signals
            if s.signal_type in ("emote_burst", "reaction_excitement")
            and payoff_start <= s.timestamp < payoff_end
        ]
        if chat_payoffs:
            chat_boost = min(20, max(s.strength for s in chat_payoffs) * 0.3)
            score = min(100, score + chat_boost)
            reasons.append("Chat celebration")

        return StoryComponent(
            name="payoff",
            score=score,
            evidence="; ".join(reasons) if reasons else "Weak payoff",
            timestamp=payoff_start,
            details={"payoff_text": payoff_text[:100], "llm_payoffs": len(llm_payoffs)}
        )

    def _score_payoff_text(self, text: str, timestamp: float) -> Tuple[float, List[str]]:
        """Score payoff quality from text."""
        text_lower = text.lower()
        score = 0.0
        reasons = []

        patterns = {
            "resolution": (["that's why", "so that's", "turns out", "ended up", "result was"], 30, "Resolution"),
            "punchline": (["lmao", "lol", "haha", "got em", "rekt", "owned", "destroyed"], 35, "Punchline"),
            "reveal": (["actually it was", "the truth", "secret was", "surprise"], 25, "Reveal"),
            "emotional": (["heart", "crying", "tears", "emotional", "touched", "wholesome"], 30, "Emotional payoff"),
            "agreement": (["exactly", "true", "facts", "agreed", "you're right"], 20, "Agreement/validation"),
            "cliffhanger": (["to be continued", "part 2", "next time", "wait for it"], 15, "Cliffhanger"),
        }

        for pattern_name, (keywords, points, reason) in patterns.items():
            if any(kw in text_lower for kw in keywords):
                score += points
                reasons.append(reason)

        # Length check
        if len(text.strip()) < 5:
            score *= 0.3
            reasons.append("Too brief")

        score = min(100, score)
        return score, reasons

    def _analyze_reaction(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis,
        audio_signals: List[AudioSignal],
        chat_signals: List[ChatSignal]
    ) -> StoryComponent:
        """Analyze reaction (emotional response after payoff)."""
        reaction_start = moment.original_end
        reaction_end = moment.expanded_end

        if reaction_end <= reaction_start:
            return StoryComponent(
                name="reaction",
                score=0,
                evidence="No post-reaction window",
                timestamp=reaction_start,
            )

        reaction_segments = [
            s for s in transcript.segments
            if s.start >= reaction_start and s.start < reaction_end
        ]

        reaction_text = " ".join(s.text for s in reaction_segments)
        score = 0.0
        reasons = []

        if reaction_text:
            reaction_lower = reaction_text.lower()
            reaction_patterns = [
                (["lol", "lmao", "haha", "hahaha", "rofl"], 25, "Laughter"),
                (["omg", "wtf", "holy", "insane", "crazy", "no way"], 25, "Shock"),
                (["that was", "this is", "bro that", "chat that"], 20, "Commentary"),
                (["clip it", "clipped", "save that", "content"], 20, "Clip request"),
                (["tears", "crying", "emotional", "wholesome", "heart"], 20, "Emotional"),
            ]
            for keywords, points, reason in reaction_patterns:
                if any(kw in reaction_lower for kw in keywords):
                    score += points
                    reasons.append(reason)

        # Audio reaction
        audio_reactions = [
            s for s in audio_signals
            if s.signal_type in ("laughter", "energy_spike")
            and reaction_start <= s.timestamp < reaction_end
        ]
        if audio_reactions:
            score += min(20, max(s.strength for s in audio_reactions) * 0.3)
            reasons.append("Audio reaction")

        # Chat reaction
        chat_reactions = [
            s for s in chat_signals
            if s.signal_type in ("emote_burst", "reaction_excitement", "repeated_phrase")
            and reaction_start <= s.timestamp < reaction_end
        ]
        if chat_reactions:
            score += min(15, max(s.strength for s in chat_reactions) * 0.2)
            reasons.append("Chat reaction")

        return StoryComponent(
            name="reaction",
            score=min(100, score),
            evidence="; ".join(reasons) if reasons else "Minimal reaction",
            timestamp=reaction_start,
            details={"reaction_text": reaction_text[:100]}
        )

    def _analyze_pacing(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis
    ) -> str:
        """Analyze pacing of the clip."""
        duration = moment.duration
        segments = [
            s for s in transcript.segments
            if s.end > moment.expanded_start and s.start < moment.expanded_end
        ]

        if not segments:
            return "unknown"

        speech_time = sum(s.end - s.start for s in segments)
        density = speech_time / duration if duration > 0 else 0

        if density > 0.85:
            return "rushed"
        elif density > 0.6:
            return "good"
        elif density > 0.4:
            return "moderate"
        else:
            return "slow"

    def _build_engagement_curve(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis,
        audio_signals: List[AudioSignal],
        chat_signals: List[ChatSignal]
    ) -> List[Tuple[float, float]]:
        """Build engagement curve over time."""
        curve = []
        duration = moment.duration
        num_points = max(10, int(duration / 2))

        for i in range(num_points + 1):
            t = moment.expanded_start + (duration * i / num_points)
            engagement = 0.0

            # Transcript engagement (keywords, questions)
            nearby_segments = [
                s for s in transcript.segments
                if abs(s.start - t) < 3.0
            ]
            if nearby_segments:
                engagement += 20

            # Audio engagement
            audio_nearby = [
                s for s in audio_signals
                if abs(s.timestamp - t) < 2.0
            ]
            if audio_nearby:
                engagement += max(s.strength * 0.3 for s in audio_nearby)

            # Chat engagement
            chat_nearby = [
                s for s in chat_signals
                if abs(s.timestamp - t) < 3.0
            ]
            if chat_nearby:
                engagement += max(s.strength * 0.2 for s in chat_nearby)

            curve.append((t - moment.expanded_start, min(100, engagement)))

        return curve

    def _generate_recommendations(
        self,
        hook: StoryComponent,
        context: StoryComponent,
        payoff: StoryComponent,
        reaction: StoryComponent,
        pacing: str
    ) -> List[str]:
        """Generate editing recommendations based on story analysis."""
        recs = []

        if hook.score < 40:
            recs.append("Consider starting clip earlier to include stronger hook")
        if context.score < 40:
            recs.append("Add more pre-context for viewer understanding")
        if payoff.score < 40:
            recs.append("Extend clip to capture payoff/reaction")
        if reaction.score < 30:
            recs.append("Include post-moment reaction for emotional closure")

        if pacing == "rushed":
            recs.append("Pacing is fast - consider keeping pauses for comedic timing")
        elif pacing == "slow":
            recs.append("Pacing is slow - aggressive silence removal recommended")

        if hook.score > 70 and payoff.score > 70:
            recs.append("Strong hook-payoff structure - good candidate for viral clip")

        return recs


def analyze_all_stories(
    moments: List[ExpandedMoment],
    transcript: TranscriptAnalysis,
    audio_signals: List[AudioSignal],
    chat_signals: List[ChatSignal],
    config: StoryConfig = None
) -> List[StoryAnalysis]:
    """Analyze story structure for all moments."""
    analyzer = StoryAnalyzer(config)
    return [analyzer.analyze(m, transcript, audio_signals, chat_signals) for m in moments]