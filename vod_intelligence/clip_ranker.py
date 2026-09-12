"""
Clip Ranker - Final ranking, deduplication, and selection of best clips.

Combines moment scores, story analysis, and duplicate detection
to produce a final ranked list of clip candidates for review.
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
import json

from .config import CONFIG, RankingConfig
from .moment_scorer import MomentScore
from .story_analyzer import StoryAnalysis
from .context_analyzer import ExpandedMoment


@dataclass
class RankedClip:
    """A fully analyzed and ranked clip candidate."""
    rank: int
    moment: ExpandedMoment
    moment_score: MomentScore
    story_analysis: StoryAnalysis
    final_score: float
    dedup_key: str
    is_duplicate: bool = False
    duplicate_of: Optional[str] = None
    review_priority: str = "normal"  # "high", "normal", "low"
    tags: List[str] = field(default_factory=list)


@dataclass
class RankingResult:
    """Complete ranking results."""
    ranked_clips: List[RankedClip]
    top_clips: List[RankedClip]
    rejected_clips: List[RankedClip]
    total_candidates: int
    duplicates_removed: int
    ranking_metadata: Dict[str, Any]


class ClipRanker:
    """
    Ranks and deduplicates clip candidates.
    """

    def __init__(self, config: RankingConfig = None):
        self.config = config or CONFIG.ranking

    def rank_clips(
        self,
        moments: List[ExpandedMoment],
        moment_scores: List[MomentScore],
        story_analyses: List[StoryAnalysis],
        clip_history: List[Dict[str, Any]] = None
    ) -> RankingResult:
        """
        Rank all clips and remove duplicates.

        Args:
            moments: Expanded moments
            moment_scores: Scores from MomentScorer
            story_analyses: Story structure analyses
            clip_history: Previous clips for cross-VOD deduplication

        Returns:
            RankingResult with ranked clips
        """
        # Combine all data
        combined = list(zip(moments, moment_scores, story_analyses))

        # Calculate final scores
        ranked = []
        for moment, m_score, story in combined:
            final_score = self._calculate_final_score(m_score, story, moment)
            dedup_key = self._generate_dedup_key(moment)

            ranked.append(RankedClip(
                rank=0,  # Will be set after sorting
                moment=moment,
                moment_score=m_score,
                story_analysis=story,
                final_score=final_score,
                dedup_key=dedup_key,
                review_priority=self._determine_priority(m_score, story),
                tags=self._generate_tags(m_score, story, moment),
            ))

        # Sort by final score
        ranked.sort(key=lambda r: r.final_score, reverse=True)

        # Deduplicate
        ranked = self._deduplicate(ranked, clip_history or [])

        # Assign ranks
        for i, clip in enumerate(ranked):
            clip.rank = i + 1

        # Split into top and rejected
        top_n = min(self.config.top_n_for_review, len(ranked))
        top_clips = ranked[:top_n]
        rejected_clips = ranked[top_n:]

        # Count duplicates
        dup_count = sum(1 for c in ranked if c.is_duplicate)

        return RankingResult(
            ranked_clips=ranked,
            top_clips=top_clips,
            rejected_clips=rejected_clips,
            total_candidates=len(moments),
            duplicates_removed=dup_count,
            ranking_metadata={
                "config_weights": {
                    "story_quality": self.config.story_quality_weight,
                    "hook": self.config.hook_weight,
                    "payoff": self.config.payoff_weight,
                    "emotional": self.config.emotional_weight,
                    "context": self.config.context_weight,
                    "novelty": self.config.novelty_weight,
                    "pacing": self.config.pacing_weight,
                },
                "dedup_threshold_time": self.config.duplicate_time_threshold,
                "dedup_threshold_text": self.config.duplicate_text_similarity,
            }
        )

    def _calculate_final_score(
        self,
        moment_score: MomentScore,
        story: StoryAnalysis,
        moment: ExpandedMoment
    ) -> float:
        """Calculate weighted final score."""
        # Base from moment scorer
        base = moment_score.total_score

        # Story quality bonus
        story_bonus = story.overall_score * self.config.story_quality_weight

        # Hook bonus
        hook_bonus = story.hook.score * self.config.hook_weight

        # Payoff bonus
        payoff_bonus = story.payoff.score * self.config.payoff_weight

        # Emotional/reaction bonus
        emotional_bonus = (
            story.reaction.score * self.config.emotional_weight +
            moment_score.emotion_score * 0.05 +
            moment_score.reaction_score * 0.05
        )

        # Context bonus
        context_bonus = moment.context.context_quality * self.config.context_weight

        # Novelty bonus
        novelty_bonus = moment_score.novelty_score * self.config.novelty_weight

        # Pacing bonus
        pacing_bonus = moment_score.pacing_score * self.config.pacing_weight

        # Complete arc bonus
        arc_bonus = 5.0 if story.has_complete_arc else 0.0

        # Duration sweet spot bonus (already in moment_score.duration_bonus)
        duration_bonus = moment_score.duration_bonus

        total = (
            base * 0.4 +  # Base score weight
            story_bonus +
            hook_bonus +
            payoff_bonus +
            emotional_bonus +
            context_bonus +
            novelty_bonus +
            pacing_bonus +
            arc_bonus +
            duration_bonus
        )

        return min(100.0, total)

    def _generate_dedup_key(self, moment: ExpandedMoment) -> str:
        """Generate a key for duplicate detection."""
        # Time-based key (rounded to nearest 5 seconds)
        time_key = f"{int(moment.original_start / 5) * 5}_{int(moment.original_end / 5) * 5}"

        # Content-based key (first 50 chars of hook + payoff)
        content = (moment.hook_text[:30] + moment.payoff_text[:30]).lower()
        content_key = "".join(c for c in content if c.isalnum())[:40]

        return f"{time_key}_{content_key}"

    def _deduplicate(
        self,
        ranked: List[RankedClip],
        clip_history: List[Dict[str, Any]]
    ) -> List[RankedClip]:
        """Remove duplicate clips."""
        seen_keys = set()
        seen_times = []  # List of (start, end) for temporal dedup
        result = []

        # Add historical clips to seen
        for hist in clip_history:
            if "start_time" in hist and "end_time" in hist:
                seen_times.append((hist["start_time"], hist["end_time"]))
            if "transcript" in hist:
                # Could add content-based historical dedup here
                pass

        for clip in ranked:
            is_dup = False
            dup_of = None

            # Temporal deduplication
            for seen_start, seen_end in seen_times:
                if self._times_overlap(
                    clip.moment.original_start, clip.moment.original_end,
                    seen_start, seen_end,
                    self.config.duplicate_time_threshold
                ):
                    is_dup = True
                    dup_of = f"historical_{seen_start:.0f}_{seen_end:.0f}"
                    break

            # Key-based deduplication (within this VOD)
            if not is_dup and clip.dedup_key in seen_keys:
                is_dup = True
                dup_of = clip.dedup_key

            # Content similarity deduplication (simplified)
            if not is_dup:
                for existing in result:
                    if self._content_similar(clip, existing):
                        is_dup = True
                        dup_of = existing.dedup_key
                        break

            if is_dup:
                clip.is_duplicate = True
                clip.duplicate_of = dup_of
                clip.review_priority = "low"
            else:
                seen_keys.add(clip.dedup_key)
                seen_times.append((clip.moment.original_start, clip.moment.original_end))
                result.append(clip)

        return result

    def _times_overlap(
        self,
        start1: float, end1: float,
        start2: float, end2: float,
        threshold: float
    ) -> bool:
        """Check if two time ranges overlap significantly."""
        overlap_start = max(start1, start2)
        overlap_end = min(end1, end2)
        overlap = max(0, overlap_end - overlap_start)

        duration1 = end1 - start1
        duration2 = end2 - start2
        min_duration = min(duration1, duration2)

        if min_duration == 0:
            return False

        overlap_ratio = overlap / min_duration
        return overlap_ratio > 0.5 or overlap > threshold

    def _content_similar(self, clip1: RankedClip, clip2: RankedClip) -> bool:
        """Check content similarity between clips."""
        # Compare hook + payoff text
        text1 = (clip1.moment.hook_text + clip1.moment.payoff_text).lower()
        text2 = (clip2.moment.hook_text + clip2.moment.payoff_text).lower()

        if not text1 or not text2:
            return False

        # Simple word overlap
        words1 = set(text1.split())
        words2 = set(text2.split())

        if not words1 or not words2:
            return False

        overlap = len(words1 & words2) / len(words1 | words2)
        return overlap >= self.config.duplicate_text_similarity

    def _determine_priority(self, moment_score: MomentScore, story: StoryAnalysis) -> str:
        """Determine review priority."""
        if moment_score.total_score >= 75 and story.has_complete_arc:
            return "high"
        elif moment_score.total_score >= 55:
            return "normal"
        else:
            return "low"

    def _generate_tags(
        self,
        moment_score: MomentScore,
        story: StoryAnalysis,
        moment: ExpandedMoment
    ) -> List[str]:
        """Generate descriptive tags for the clip."""
        tags = []

        # Duration tags
        if moment.duration <= 15:
            tags.append("short")
        elif moment.duration <= 30:
            tags.append("medium")
        elif moment.duration <= 60:
            tags.append("long")
        else:
            tags.append("extended")

        # Content tags
        if moment_score.hook_score > 60:
            tags.append("strong-hook")
        if moment_score.emotion_score > 60:
            tags.append("emotional")
        if moment_score.humor_score > 60:
            tags.append("funny")
        if moment_score.surprise_score > 60:
            tags.append("surprising")
        if moment_score.story_score > 60:
            tags.append("story-driven")
        if moment_score.reaction_score > 60:
            tags.append("reaction-heavy")

        # Story tags
        if story.has_complete_arc:
            tags.append("complete-story")
        if story.pacing_rating == "good":
            tags.append("well-paced")
        elif story.pacing_rating == "rushed":
            tags.append("fast-paced")

        # Technical tags
        if moment.context.context_quality > 70:
            tags.append("well-contextualized")

        return tags

    def export_for_review(self, result: RankingResult, output_path: Path):
        """Export ranking results for human review UI."""
        data = {
            "metadata": result.ranking_metadata,
            "summary": {
                "total_candidates": result.total_candidates,
                "top_clips": len(result.top_clips),
                "rejected": len(result.rejected_clips),
                "duplicates_removed": result.duplicates_removed,
            },
            "top_clips": [self._clip_to_dict(c) for c in result.top_clips],
            "all_clips": [self._clip_to_dict(c) for c in result.ranked_clips],
        }
        output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')

    def _clip_to_dict(self, clip: RankedClip) -> Dict[str, Any]:
        """Convert RankedClip to dictionary for JSON export."""
        return {
            "rank": clip.rank,
            "start_time": clip.moment.original_start,
            "end_time": clip.moment.original_end,
            "expanded_start": clip.moment.expanded_start,
            "expanded_end": clip.moment.expanded_end,
            "duration": clip.moment.duration,
            "final_score": round(clip.final_score, 2),
            "moment_score": clip.moment_score.to_dict(),
            "story_score": round(clip.story_analysis.overall_score, 2),
            "has_complete_arc": clip.story_analysis.has_complete_arc,
            "pacing": clip.story_analysis.pacing_rating,
            "hook_text": clip.moment.hook_text[:200],
            "payoff_text": clip.moment.payoff_text[:200],
            "context_text": clip.moment.context_text[:200],
            "reaction_text": clip.moment.reaction_text[:200],
            "review_priority": clip.review_priority,
            "tags": clip.tags,
            "is_duplicate": clip.is_duplicate,
            "duplicate_of": clip.duplicate_of,
            "edit_recommendations": clip.moment.edit_recommendations,
            "story_recommendations": clip.story_analysis.recommendations,
        }


def create_review_summary(result: RankingResult) -> str:
    """Create a human-readable summary for Discord review."""
    lines = [
        f"🎬 **VOD Analysis Complete**",
        f"",
        f"📊 **Candidates Found**: {result.total_candidates}",
        f"⭐ **Top Clips for Review**: {len(result.top_clips)}",
        f"🗑️ **Duplicates Removed**: {result.duplicates_removed}",
        f"",
        f"**Top {len(result.top_clips)} Clips:**",
        f"",
    ]

    for clip in result.top_clips:
        m = clip.moment
        lines.extend([
            f"**#{clip.rank}** (Score: {clip.final_score:.1f}) [{clip.review_priority.upper()}]",
            f"  ⏱️ `{m.expanded_start:.1f}s - {m.expanded_end:.1f}s` ({m.duration:.1f}s)",
            f"  🎯 Hook: {m.hook_text[:80]}...",
            f"  💰 Payoff: {m.payoff_text[:80]}...",
            f"  📖 Story: {'Complete' if clip.story_analysis.has_complete_arc else 'Incomplete'} | Pacing: {clip.story_analysis.pacing_rating}",
            f"  🏷️ Tags: {', '.join(clip.tags[:5])}",
            f"",
        ])

    return "\n".join(lines)