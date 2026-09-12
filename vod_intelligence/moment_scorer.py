"""
Moment Scorer - Configurable multi-signal scoring for candidate moments.

Combines signals from:
- Audio (energy, laughter, shifts)
- Transcript (keywords, emotion, storytelling, hooks, payoffs)
- Chat (spikes, emotes, reactions)
- Visual (scene changes, face reactions) - optional
- Context quality
- Story structure

Produces a normalized 0-100 score with component breakdown.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from collections import defaultdict
import math

from .config import CONFIG, ScoringWeights, DEFAULT_SIGNAL_WEIGHTS
from .audio_analyzer import AudioSignal
from .chat_analyzer import ChatSignal
from .transcript_analyzer import TranscriptAnalysis
from .context_analyzer import ExpandedMoment


@dataclass
class SignalScore:
    """Score for a single signal type."""
    signal_name: str
    raw_score: float  # 0-100
    weighted_score: float
    weight: float
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class MomentScore:
    """Complete score breakdown for a moment."""
    moment_id: str
    total_score: float
    component_scores: Dict[str, float]
    signal_scores: List[SignalScore]
    hook_score: float
    emotion_score: float
    reaction_score: float
    surprise_score: float
    humor_score: float
    story_score: float
    transcript_score: float
    chat_score: float
    novelty_score: float
    pacing_score: float
    context_quality: float
    duration_bonus: float
    audio_energy: float = 0.0
    explanation: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "moment_id": self.moment_id,
            "total_score": round(self.total_score, 2),
            "components": {k: round(v, 2) for k, v in self.component_scores.items()},
            "signals": [
                {"name": s.signal_name, "raw": round(s.raw_score, 2), "weighted": round(s.weighted_score, 2), "weight": s.weight}
                for s in self.signal_scores
            ],
            "hook": round(self.hook_score, 2),
            "emotion": round(self.emotion_score, 2),
            "reaction": round(self.reaction_score, 2),
            "surprise": round(self.surprise_score, 2),
            "humor": round(self.humor_score, 2),
            "story": round(self.story_score, 2),
            "transcript": round(self.transcript_score, 2),
            "chat": round(self.chat_score, 2),
            "novelty": round(self.novelty_score, 2),
            "pacing": round(self.pacing_score, 2),
            "context": round(self.context_quality, 2),
            "duration_bonus": round(self.duration_bonus, 2),
            "audio_energy": round(self.audio_energy, 2),
            "explanation": self.explanation,
        }


class MomentScorer:
    """
    Scores candidate moments using configurable multi-signal model.
    """

    def __init__(self, weights: ScoringWeights = None, signal_weights: Dict[str, float] = None):
        self.weights = weights or CONFIG.scoring
        self.signal_weights = signal_weights or DEFAULT_SIGNAL_WEIGHTS

    def score_moment(
        self,
        moment: ExpandedMoment,
        transcript: TranscriptAnalysis,
        audio_signals: List[AudioSignal],
        chat_signals: List[ChatSignal],
        visual_signals: List[Any] = None,
        all_moments: List[ExpandedMoment] = None
    ) -> MomentScore:
        """
        Score a single moment using all available signals.
        """
        signal_scores = []

        # 1. Audio signals
        audio_scores = self._score_audio(moment, audio_signals)
        signal_scores.extend(audio_scores)

        # 2. Transcript signals
        transcript_scores = self._score_transcript(moment, transcript)
        signal_scores.extend(transcript_scores)

        # 3. Chat signals
        chat_scores = self._score_chat(moment, chat_signals)
        signal_scores.extend(chat_scores)

        # 4. Visual signals (if available)
        if visual_signals:
            visual_scores = self._score_visual(moment, visual_signals)
            signal_scores.extend(visual_scores)

        # 5. Context quality
        context_score = self._score_context(moment)
        signal_scores.append(SignalScore(
            signal_name="context_quality",
            raw_score=context_score,
            weighted_score=context_score * self.signal_weights.get("context_quality", 0.05),
            weight=self.signal_weights.get("context_quality", 0.05),
        ))

        # 6. Novelty (vs other moments)
        novelty_score = self._score_novelty(moment, all_moments or [])
        signal_scores.append(SignalScore(
            signal_name="novelty",
            raw_score=novelty_score,
            weighted_score=novelty_score * self.signal_weights.get("novelty", 0.03),
            weight=self.signal_weights.get("novelty", 0.03),
        ))

        # 7. Pacing
        pacing_score = self._score_pacing(moment, transcript)
        signal_scores.append(SignalScore(
            signal_name="pacing",
            raw_score=pacing_score,
            weighted_score=pacing_score * self.signal_weights.get("pacing", 0.02),
            weight=self.signal_weights.get("pacing", 0.02),
        ))

        # 8. Duration bonus (sweet spot for short-form)
        duration_bonus = self._calculate_duration_bonus(moment.duration)

        # Calculate component scores (aligned with ScoringWeights)
        component_scores = self._aggregate_components(signal_scores, duration_bonus)

        # Total score
        total = sum(component_scores.values()) + duration_bonus
        total = min(100.0, total)

        # Generate explanation
        explanation = self._generate_explanation(component_scores, signal_scores, moment, duration_bonus)

        audio_energy = 0.0
        for s in signal_scores:
            if s.signal_name == "audio_energy":
                audio_energy = s.raw_score
                break

        return MomentScore(
            moment_id=f"moment_{moment.original_start:.1f}_{moment.original_end:.1f}",
            total_score=total,
            component_scores=component_scores,
            signal_scores=signal_scores,
            hook_score=component_scores.get("hook", 0),
            emotion_score=component_scores.get("emotional_intensity", 0),
            reaction_score=component_scores.get("reaction", 0),
            surprise_score=component_scores.get("surprise", 0),
            humor_score=component_scores.get("humor", 0),
            story_score=component_scores.get("story_value", 0),
            transcript_score=component_scores.get("transcript_relevance", 0),
            chat_score=component_scores.get("chat_reaction", 0),
            novelty_score=component_scores.get("novelty", 0),
            pacing_score=component_scores.get("pacing", 0),
            context_quality=context_score,
            duration_bonus=duration_bonus,
            audio_energy=audio_energy,
            explanation=explanation,
        )

    def _score_audio(self, moment: ExpandedMoment, audio_signals: List[AudioSignal]) -> List[SignalScore]:
        """Score audio signals near the moment."""
        scores = []
        radius = 15.0

        # Energy spike
        energy_signals = [s for s in audio_signals
                         if s.signal_type == "energy_spike"
                         and abs(s.timestamp - moment.original_start) <= radius]
        if energy_signals:
            max_strength = max(s.strength for s in energy_signals)
            scores.append(SignalScore(
                signal_name="audio_energy",
                raw_score=max_strength,
                weighted_score=max_strength * self.signal_weights.get("audio_energy", 0.12),
                weight=self.signal_weights.get("audio_energy", 0.12),
                details={"count": len(energy_signals), "max_strength": max_strength}
            ))

        # Laughter
        laughter_signals = [s for s in audio_signals
                           if s.signal_type == "laughter"
                           and abs(s.timestamp - moment.original_start) <= radius]
        if laughter_signals:
            max_strength = max(s.strength for s in laughter_signals)
            scores.append(SignalScore(
                signal_name="audio_laughter",
                raw_score=max_strength,
                weighted_score=max_strength * self.signal_weights.get("audio_laughter", 0.10),
                weight=self.signal_weights.get("audio_laughter", 0.10),
                details={"count": len(laughter_signals), "max_confidence": max(s.details.get("confidence", 0) for s in laughter_signals)}
            ))

        # Energy shift (sudden changes)
        shift_signals = [s for s in audio_signals
                        if s.signal_type in ("energy_shift_up", "energy_shift_down")
                        and abs(s.timestamp - moment.original_start) <= radius]
        if shift_signals:
            max_strength = max(s.strength for s in shift_signals)
            scores.append(SignalScore(
                signal_name="audio_energy_shift",
                raw_score=max_strength,
                weighted_score=max_strength * self.signal_weights.get("audio_energy_shift", 0.08),
                weight=self.signal_weights.get("audio_energy_shift", 0.08),
                details={"count": len(shift_signals), "types": [s.signal_type for s in shift_signals]}
            ))

        # Speech burst
        speech_signals = [s for s in audio_signals
                         if s.signal_type == "speech_burst"
                         and abs(s.timestamp - moment.original_start) <= radius]
        if speech_signals:
            max_strength = max(s.strength for s in speech_signals)
            scores.append(SignalScore(
                signal_name="audio_speech_burst",
                raw_score=max_strength,
                weighted_score=max_strength * self.signal_weights.get("audio_speech_burst", 0.05),
                weight=self.signal_weights.get("audio_speech_burst", 0.05),
            ))

        return scores

    def _score_transcript(self, moment: ExpandedMoment, transcript: TranscriptAnalysis) -> List[SignalScore]:
        """Score transcript-based signals."""
        scores = []

        # Get transcript segments in moment range
        moment_segments = [
            s for s in transcript.segments
            if s.end > moment.original_start and s.start < moment.original_end
        ]
        moment_text = " ".join(s.text for s in moment_segments)

        # Keyword/emotion signals from LLM analysis
        if transcript.emotions:
            max_emotion = max(e.get("intensity", 0) for e in transcript.emotions)
            scores.append(SignalScore(
                signal_name="transcript_emotion",
                raw_score=max_emotion,
                weighted_score=max_emotion * self.signal_weights.get("transcript_emotion", 0.12),
                weight=self.signal_weights.get("transcript_emotion", 0.12),
                details={"emotions": transcript.emotions[:3]}
            ))

        # Storytelling
        if transcript.storytelling_score > 0:
            scores.append(SignalScore(
                signal_name="transcript_storytelling",
                raw_score=transcript.storytelling_score,
                weighted_score=transcript.storytelling_score * self.signal_weights.get("transcript_storytelling", 0.10),
                weight=self.signal_weights.get("transcript_storytelling", 0.10),
            ))

        # Hook candidates in this moment
        hooks_in_moment = [
            h for h in transcript.hook_candidates
            if abs(h.get("timestamp", 0) - moment.original_start) <= 10
        ]
        if hooks_in_moment:
            max_hook = max(h.get("strength", 0) for h in hooks_in_moment)
            scores.append(SignalScore(
                signal_name="transcript_keywords",  # Hook strength maps to keywords
                raw_score=max_hook,
                weighted_score=max_hook * self.signal_weights.get("transcript_keywords", 0.15),
                weight=self.signal_weights.get("transcript_keywords", 0.15),
                details={"hook_count": len(hooks_in_moment), "best_hook": hooks_in_moment[0].get("text", "")[:50]}
            ))

        # Questions
        questions_in_moment = [
            q for q in transcript.questions
            if abs(q.get("timestamp", 0) - moment.original_start) <= 10
        ]
        if questions_in_moment:
            scores.append(SignalScore(
                signal_name="transcript_questions",
                raw_score=min(100, len(questions_in_moment) * 15),
                weighted_score=min(100, len(questions_in_moment) * 15) * self.signal_weights.get("transcript_questions", 0.05),
                weight=self.signal_weights.get("transcript_questions", 0.05),
                details={"count": len(questions_in_moment)}
            ))

        # Controversy
        if transcript.controversy_score > 0:
            scores.append(SignalScore(
                signal_name="transcript_controversy",
                raw_score=transcript.controversy_score,
                weighted_score=transcript.controversy_score * self.signal_weights.get("transcript_controversy", 0.08),
                weight=self.signal_weights.get("transcript_controversy", 0.08),
            ))

        # Surprise
        if transcript.surprise_score > 0:
            scores.append(SignalScore(
                signal_name="surprise",
                raw_score=transcript.surprise_score,
                weighted_score=transcript.surprise_score * self.signal_weights.get("surprise", 0.10),
                weight=self.signal_weights.get("surprise", 0.10),
            ))

        return scores

    def _score_chat(self, moment: ExpandedMoment, chat_signals: List[ChatSignal]) -> List[SignalScore]:
        """Score chat signals near the moment."""
        scores = []
        radius = 15.0

        # Chat spike
        spike_signals = [s for s in chat_signals
                        if s.signal_type == "spike"
                        and abs(s.timestamp - moment.original_start) <= radius]
        if spike_signals:
            max_strength = max(s.strength for s in spike_signals)
            scores.append(SignalScore(
                signal_name="chat_spike",
                raw_score=max_strength,
                weighted_score=max_strength * self.signal_weights.get("chat_spike", 0.10),
                weight=self.signal_weights.get("chat_spike", 0.10),
                details={"count": len(spike_signals), "max_rate": max(s.details.get("rate", 0) for s in spike_signals)}
            ))

        # Emote burst
        emote_signals = [s for s in chat_signals
                        if s.signal_type == "emote_burst"
                        and abs(s.timestamp - moment.original_start) <= radius]
        if emote_signals:
            max_strength = max(s.strength for s in emote_signals)
            scores.append(SignalScore(
                signal_name="chat_emote_burst",
                raw_score=max_strength,
                weighted_score=max_strength * self.signal_weights.get("chat_emote_burst", 0.05),
                weight=self.signal_weights.get("chat_emote_burst", 0.05),
                details={"count": len(emote_signals)}
            ))

        # Repeated phrases
        phrase_signals = [s for s in chat_signals
                         if s.signal_type == "repeated_phrase"
                         and abs(s.timestamp - moment.original_start) <= radius]
        if phrase_signals:
            max_strength = max(s.strength for s in phrase_signals)
            scores.append(SignalScore(
                signal_name="chat_repeated_phrases",
                raw_score=max_strength,
                weighted_score=max_strength * self.signal_weights.get("chat_repeated_phrases", 0.05),
                weight=self.signal_weights.get("chat_repeated_phrases", 0.05),
                details={"phrases": [s.details.get("phrase", "") for s in phrase_signals[:3]]}
            ))

        # Reactions (excitement/confusion)
        reaction_signals = [s for s in chat_signals
                           if s.signal_type in ("reaction_excitement", "reaction_confusion")
                           and abs(s.timestamp - moment.original_start) <= radius]
        if reaction_signals:
            max_strength = max(s.strength for s in reaction_signals)
            scores.append(SignalScore(
                signal_name="chat_reaction",
                raw_score=max_strength,
                weighted_score=max_strength * 0.05,  # Additional reaction weight
                weight=0.05,
                details={"types": [s.signal_type for s in reaction_signals]}
            ))

        return scores

    def _score_visual(self, moment: ExpandedMoment, visual_signals: List[Any]) -> List[SignalScore]:
        """Score visual signals (placeholder for future CV integration)."""
        # TODO: Implement when visual analysis is added
        return []

    def _score_context(self, moment: ExpandedMoment) -> float:
        """Score context quality."""
        return moment.context.context_quality

    def _score_novelty(self, moment: ExpandedMoment, all_moments: List[ExpandedMoment]) -> float:
        """Score novelty compared to other moments."""
        if not all_moments or len(all_moments) <= 1:
            return 50.0  # Neutral if no comparison

        # Check temporal proximity
        for other in all_moments:
            if other is moment:
                continue
            time_diff = abs(other.original_start - moment.original_start)
            if time_diff < 30:  # Within 30 seconds
                return 20.0  # Low novelty

        # Check content similarity (simplified - would use embeddings in production)
        return 80.0  # High novelty if temporally distinct

    def _score_pacing(self, moment: ExpandedMoment, transcript: TranscriptAnalysis) -> float:
        """Score pacing quality."""
        duration = moment.duration
        segments = [
            s for s in transcript.segments
            if s.end > moment.expanded_start and s.start < moment.expanded_end
        ]

        if not segments:
            return 50.0

        # Speech density
        total_speech = sum(s.end - s.start for s in segments)
        density = total_speech / duration if duration > 0 else 0

        # Optimal density for short-form: 0.6-0.8
        if 0.6 <= density <= 0.8:
            return 100.0
        elif 0.4 <= density < 0.6:
            return 70.0
        elif 0.8 < density <= 0.9:
            return 80.0
        else:
            return 40.0

    def _calculate_duration_bonus(self, duration: float) -> float:
        """Bonus for optimal short-form durations."""
        # TikTok/Reels sweet spots
        if 15 <= duration <= 30:
            return 8.0
        elif 30 < duration <= 45:
            return 5.0
        elif 45 < duration <= 60:
            return 3.0
        elif 10 <= duration < 15:
            return 5.0
        elif 60 < duration <= 90:
            return 2.0
        else:
            return 0.0

    def _aggregate_components(
        self,
        signal_scores: List[SignalScore],
        duration_bonus: float
    ) -> Dict[str, float]:
        """Map signal scores to component scores."""
        components = defaultdict(float)

        # Map signals to components
        signal_to_component = {
            "audio_energy": "emotional_intensity",
            "audio_laughter": "humor",
            "audio_energy_shift": "surprise",
            "audio_speech_burst": "pacing",
            "transcript_emotion": "emotional_intensity",
            "transcript_storytelling": "story_value",
            "transcript_keywords": "hook",
            "transcript_questions": "hook",
            "transcript_controversy": "surprise",
            "surprise": "surprise",
            "chat_spike": "reaction",
            "chat_emote_burst": "reaction",
            "chat_repeated_phrases": "reaction",
            "chat_reaction": "reaction",
            "context_quality": "context_quality",
            "novelty": "novelty",
            "pacing": "pacing",
        }

        for ss in signal_scores:
            component = signal_to_component.get(ss.signal_name, "transcript_relevance")
            components[component] = max(components[component], ss.weighted_score)

        # Apply main weights
        weighted = {}
        for comp, weight in self.weights.__dict__.items():
            if comp in components:
                weighted[comp] = components[comp] * weight
            else:
                weighted[comp] = 0.0

        return weighted

    def _generate_explanation(
        self,
        components: Dict[str, float],
        signals: List[SignalScore],
        moment: ExpandedMoment,
        duration_bonus: float = 0.0
    ) -> str:
        """Generate human-readable explanation of the score."""
        parts = []

        # Top contributing components
        sorted_comps = sorted(components.items(), key=lambda x: x[1], reverse=True)
        top_comps = [(k, v) for k, v in sorted_comps if v > 2.0][:4]

        for comp, score in top_comps:
            comp_name = comp.replace("_", " ").title()
            parts.append(f"{comp_name}: {score:.1f}")

        # Key signals
        top_signals = sorted(signals, key=lambda s: s.weighted_score, reverse=True)[:3]
        for sig in top_signals:
            if sig.weighted_score > 1.0:
                parts.append(f"{sig.signal_name} ({sig.raw_score:.0f})")

        # Duration
        parts.append(f"Duration: {moment.duration:.1f}s ({'+' if duration_bonus > 0 else ''}{duration_bonus:.1f})")

        # Context
        ctx = getattr(moment, 'context', moment)
        context_quality = getattr(ctx, 'context_quality', 50)
        if context_quality > 70:
            parts.append(f"Strong context ({context_quality:.0f})")
        elif context_quality < 40:
            parts.append(f"Weak context ({context_quality:.0f})")

        return " | ".join(parts)


def score_all_moments(
    moments: List[ExpandedMoment],
    transcript: TranscriptAnalysis,
    audio_signals: List[AudioSignal],
    chat_signals: List[ChatSignal],
    visual_signals: List[Any] = None,
    weights: ScoringWeights = None
) -> List[MomentScore]:
    """Score all moments and return sorted by score."""
    scorer = MomentScorer(weights)
    all_scores = []

    for moment in moments:
        score = scorer.score_moment(
            moment, transcript, audio_signals, chat_signals, visual_signals, moments
        )
        all_scores.append(score)

    # Sort by total score descending
    all_scores.sort(key=lambda s: s.total_score, reverse=True)
    return all_scores