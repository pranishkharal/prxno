"""
Pipeline Orchestrator - Coordinates the full VOD analysis pipeline.

Orchestrates:
1. Download VOD
2. Probe media
3. Transcribe audio
4. Analyze chat (if available)
5. Analyze audio signals
6. Analyze transcript semantics
7. Detect candidate moments
8. Expand moments with context
9. Score moments
10. Analyze story structure
11. Rank and deduplicate
12. Generate metadata
13. Create edit plans
14. Present for review
"""

import asyncio
import json
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Any, Callable
from dataclasses import asdict

from .job_manager import JobManager, VODJob, JobState, JobErrorType, ClipCandidate
from .vod_downloader import download_kick_vod, probe_media, cleanup_download
from .chat_analyzer import ChatAnalyzer, fetch_kick_chat_replay, create_mock_chat
from .audio_analyzer import AudioAnalyzer
from .transcript_analyzer import TranscriptAnalyzer
from .context_analyzer import ContextAnalyzer, merge_overlapping_moments, ExpandedMoment
from .moment_scorer import score_all_moments
from .story_analyzer import analyze_all_stories
from .clip_ranker import ClipRanker, RankingResult
from .edit_planner import EditPlanner
from .metadata_generator import MetadataGenerator, generate_metadata_for_all_clips
from .config import CONFIG, ContentType, CONTENT_TYPE_CONFIGS, ContentType, CONTENT_TYPE_CONFIGS

# Import smart_moments from parent directory
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
import smart_moments


class PipelineOrchestrator:
    """
    Orchestrates the complete VOD-to-clips pipeline.
    """

    def __init__(self, job_manager: JobManager):
        self.job_manager = job_manager
        self.chat_analyzer = ChatAnalyzer()
        self.audio_analyzer = AudioAnalyzer()
        self.transcript_analyzer = TranscriptAnalyzer()
        self.context_analyzer = ContextAnalyzer()
        self.clip_ranker = ClipRanker()
        self.edit_planner = EditPlanner()
        self.metadata_generator = MetadataGenerator()

    async def run_full_pipeline(
        self,
        job_id: str,
        progress_callback: Optional[Callable[[str, float], None]] = None
    ) -> RankingResult:
        """
        Run the complete analysis pipeline for any content type.

        Args:
            job_id: Job ID to process
            progress_callback: Optional callback(step_name, progress_percent)

        Returns:
            RankingResult with ranked clips ready for review
        """
        job = self.job_manager.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        # Branch based on content type
        if job.content_type == ContentType.CLIP:
            return await self.run_clip_pipeline(job_id, progress_callback)
        elif job.content_type == ContentType.LIVE:
            return await self.run_live_pipeline(job_id, progress_callback)
        else:
            return await self.run_vod_pipeline(job_id, progress_callback)

    async def run_vod_pipeline(
        self,
        job_id: str,
        progress_callback: Optional[Callable[[str, float], None]] = None
    ) -> RankingResult:
        """Run pipeline for VOD content (download + analyze)."""
        job = self.job_manager.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        try:
            # Step 1: Download VOD
            await self._report(job_id, "Downloading VOD...", 5)
            vod_path, duration, title, streamer = await download_kick_vod(
                job.vod_url, job_id, self.job_manager
            )
            self.job_manager.set_vod_info(job_id, str(vod_path), duration, title, streamer)
            job.vod_path = str(vod_path)
            job.vod_duration = duration
            job.vod_title = title
            job.streamer_name = streamer

            # Step 2: Probe media
            await self._report(job_id, "Probing media...", 8)
            media_info = await probe_media(vod_path, job_id, self.job_manager)

            # Step 3: Transcribe
            await self._report(job_id, "Transcribing audio...", 15)
            transcript = await self.transcript_analyzer.transcribe(
                vod_path, job_id, self.job_manager
            )

            # Save transcript
            transcript_path = CONFIG.paths.analysis_cache_dir / f"transcript_{job_id}.json"
            self.transcript_analyzer.save_analysis(transcript, transcript_path)
            self.job_manager.set_transcript_path(job_id, str(transcript_path))

            # Step 4: Fetch/analyze chat
            await self._report(job_id, "Analyzing chat...", 30)
            chat_signals = await self._analyze_chat(job_id, job.vod_url, duration)

            # Step 5: Analyze audio
            await self._report(job_id, "Analyzing audio signals...", 40)
            audio_signals = await self.audio_analyzer.analyze(
                vod_path, job_id, self.job_manager
            )

            # Step 6: Detect candidate moments (using existing smart_moments)
            await self._report(job_id, "Detecting candidate moments...", 50)
            raw_moments = await self._detect_candidate_moments(transcript, audio_signals, chat_signals, duration)

            # Step 7: Expand moments with context
            await self._report(job_id, "Expanding moments with context...", 60)
            expanded_moments = self._expand_moments(
                raw_moments, transcript, audio_signals, chat_signals, duration
            )

            # Step 8: Score moments
            await self._report(job_id, "Scoring moments...", 70)
            moment_scores = score_all_moments(
                expanded_moments, transcript, audio_signals, chat_signals
            )

            # Step 9: Analyze story structure
            await self._report(job_id, "Analyzing story structure...", 75)
            story_analyses = analyze_all_stories(
                expanded_moments, transcript, audio_signals, chat_signals
            )

            # Step 10: Rank and deduplicate
            await self._report(job_id, "Ranking clips...", 80)
            clip_history = self._load_clip_history()
            ranking_result = self.clip_ranker.rank_clips(
                expanded_moments, moment_scores, story_analyses, clip_history
            )

            # Step 11: Generate metadata
            await self._report(job_id, "Generating metadata...", 85)
            metadata_list = await generate_metadata_for_all_clips(
                ranking_result.top_clips, transcript, streamer, title, job.vod_url
            )
            metadata_map = {f"rank_{m.rank}": m for m in metadata_list}

            # Step 12: Create edit plans
            await self._report(job_id, "Creating edit plans...", 90)
            for clip in ranking_result.top_clips:
                plan = self.edit_planner.create_plan(
                    clip.moment, clip.moment_score, clip.story_analysis,
                    transcript, streamer, title
                )
                clip.moment.edit_recommendations = plan.to_options_dict()
                clip.edit_plan = plan

            # Save analysis results
            analysis_data = {
                "transcript": asdict(transcript),
                "audio_signals": [asdict(s) for s in audio_signals],
                "chat_signals": [asdict(s) for s in chat_signals],
                "moments": [asdict(m) for m in expanded_moments],
                "scores": [s.to_dict() for s in moment_scores],
                "stories": [
                    {
                        "hook": asdict(s.hook),
                        "context": asdict(s.context),
                        "payoff": asdict(s.payoff),
                        "reaction": asdict(s.reaction),
                        "overall_score": s.overall_score,
                        "has_complete_arc": s.has_complete_arc,
                        "pacing_rating": s.pacing_rating,
                        "recommendations": s.recommendations,
                    }
                    for s in story_analyses
                ],
                "ranking": {
                    "top_clips": [self.clip_ranker._clip_to_dict(c) for c in ranking_result.top_clips],
                    "metadata": {k: asdict(v) for k, v in metadata_map.items()},
                }
            }
            analysis_path = CONFIG.paths.analysis_cache_dir / f"analysis_{job_id}.json"
            analysis_path.write_text(json.dumps(analysis_data, indent=2, default=str), encoding='utf-8')
            self.job_manager.set_analysis_path(job_id, str(analysis_path))

            # Step 13: Convert to ClipCandidates for job
            await self._report(job_id, "Preparing for review...", 95)
            candidates = self._convert_to_candidates(ranking_result.top_clips, metadata_map)
            self.job_manager.set_candidates(job_id, candidates)

            # Complete
            await self._report(job_id, "Ready for review!", 100)
            self.job_manager.update_state(job_id, JobState.CANDIDATES_FOUND, 100, "Ready for review")

            return ranking_result

        except Exception as e:
            traceback.print_exc()
            self.job_manager.set_error(job_id, JobErrorType.ANALYSIS_ERROR, str(e), traceback.format_exc())
            raise

    async def run_clip_pipeline(
        self,
        job_id: str,
        progress_callback: Optional[Callable[[str, float], None]] = None
    ) -> RankingResult:
        """Run pipeline for short clip content (file already downloaded)."""
        job = self.job_manager.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        try:
            vod_path = Path(job.vod_path)
            duration = job.vod_duration
            streamer = job.streamer_name
            title = job.vod_title

            # Step 1: Probe media
            await self._report(job_id, "Probing clip media...", 5)
            media_info = await probe_media(vod_path, job_id, self.job_manager)

            # Step 2: Transcribe (fast mode for clips - skip Ollama LLM)
            await self._report(job_id, "Transcribing clip audio...", 20)
            transcript = await self.transcript_analyzer.transcribe(
                vod_path, job_id, self.job_manager,
                fast_mode=True
            )

            # Save transcript
            transcript_path = CONFIG.paths.analysis_cache_dir / f"transcript_{job_id}.json"
            self.transcript_analyzer.save_analysis(transcript, transcript_path)
            self.job_manager.set_transcript_path(job_id, str(transcript_path))

            # Step 3: Analyze audio
            await self._report(job_id, "Analyzing clip audio...", 40)
            audio_signals = await self.audio_analyzer.analyze(
                vod_path, job_id, self.job_manager
            )

            # Step 4: Detect candidate moments (for clips, the whole thing is the moment)
            await self._report(job_id, "Analyzing clip content...", 60)
            chat_signals = []
            if job.content_type == ContentType.LIVE:
                chat_signals = await self._analyze_chat(job_id, job.vod_url, duration)

            raw_moments = await self._detect_candidate_moments(transcript, audio_signals, chat_signals, duration)

            # Step 5: Expand moments with context
            await self._report(job_id, "Expanding clip context...", 70)
            expanded_moments = self._expand_moments(
                raw_moments, transcript, audio_signals, chat_signals, duration
            )

            # Step 6: Score moments
            await self._report(job_id, "Scoring clip...", 80)
            moment_scores = score_all_moments(
                expanded_moments, transcript, audio_signals, chat_signals
            )

            # Step 7: Analyze story structure
            await self._report(job_id, "Analyzing story structure...", 85)
            story_analyses = analyze_all_stories(
                expanded_moments, transcript, audio_signals, chat_signals
            )

            # Step 8: Rank
            await self._report(job_id, "Ranking...", 90)
            clip_history = self._load_clip_history() if job.content_type != ContentType.CLIP else []
            ranking_result = self.clip_ranker.rank_clips(
                expanded_moments, moment_scores, story_analyses, clip_history
            )

            # Step 9: Generate metadata
            await self._report(job_id, "Generating metadata...", 95)
            metadata_list = await generate_metadata_for_all_clips(
                ranking_result.top_clips, transcript, streamer, title, job.vod_url
            )
            metadata_map = {f"rank_{m.rank}": m for m in metadata_list}

            # Step 10: Create edit plans
            for clip in ranking_result.top_clips:
                plan = self.edit_planner.create_plan(
                    clip.moment, clip.moment_score, clip.story_analysis,
                    transcript, streamer, title
                )
                clip.moment.edit_recommendations = plan.to_options_dict()
                clip.edit_plan = plan

            # Save analysis
            analysis_data = {
                "transcript": asdict(transcript),
                "audio_signals": [asdict(s) for s in audio_signals],
                "chat_signals": [asdict(s) for s in chat_signals],
                "moments": [asdict(m) for m in expanded_moments],
                "scores": [s.to_dict() for s in moment_scores],
                "stories": [
                    {
                        "hook": asdict(s.hook),
                        "context": asdict(s.context),
                        "payoff": asdict(s.payoff),
                        "reaction": asdict(s.reaction),
                        "overall_score": s.overall_score,
                        "has_complete_arc": s.has_complete_arc,
                        "pacing_rating": s.pacing_rating,
                        "recommendations": s.recommendations,
                    }
                    for s in story_analyses
                ],
                "ranking": {
                    "top_clips": [self.clip_ranker._clip_to_dict(c) for c in ranking_result.top_clips],
                    "metadata": {k: asdict(v) for k, v in metadata_map.items()},
                }
            }
            analysis_path = CONFIG.paths.analysis_cache_dir / f"analysis_{job_id}.json"
            analysis_path.write_text(json.dumps(analysis_data, indent=2, default=str), encoding='utf-8')
            self.job_manager.set_analysis_path(job_id, str(analysis_path))

            # Convert to candidates
            await self._report(job_id, "Preparing for review...", 98)
            candidates = self._convert_to_candidates(ranking_result.top_clips, metadata_map)
            self.job_manager.set_candidates(job_id, candidates)

            await self._report(job_id, "Ready for review!", 100)
            self.job_manager.update_state(job_id, JobState.CANDIDATES_FOUND, 100, "Ready for review")

            return ranking_result

        except Exception as e:
            traceback.print_exc()
            self.job_manager.set_error(job_id, JobErrorType.ANALYSIS_ERROR, str(e), traceback.format_exc())
            raise

    async def run_live_pipeline(
        self,
        job_id: str,
        progress_callback: Optional[Callable[[str, float], None]] = None
    ) -> RankingResult:
        """Run pipeline for live stream content (buffered)."""
        job = self.job_manager.get_job(job_id)
        if not job:
            raise ValueError(f"Job {job_id} not found")

        try:
            # For live streams, we buffer the stream first
            await self._report(job_id, "Buffering live stream...", 5)
            vod_path, duration, title, streamer = await download_kick_vod(
                job.vod_url, job_id, self.job_manager
            )
            self.job_manager.set_vod_info(job_id, str(vod_path), duration, title, streamer)
            job.vod_path = str(vod_path)
            job.vod_duration = duration
            job.vod_title = title
            job.streamer_name = streamer

            # Then run the same analysis pipeline
            await self._report(job_id, "Analyzing live stream...", 10)
            return await self.run_vod_pipeline(job_id, progress_callback)

        except Exception as e:
            traceback.print_exc()
            self.job_manager.set_error(job_id, JobErrorType.ANALYSIS_ERROR, str(e), traceback.format_exc())
            raise

    async def _report(self, job_id: str, step: str, progress: float):
        """Report progress."""
        self.job_manager.update_progress(job_id, progress, step)

    async def _analyze_chat(self, job_id: str, vod_url: str, duration: float) -> List:
        """Analyze chat for signals."""
        chat_signals = []

        # Try to fetch real chat
        chat_path = await fetch_kick_chat_replay(vod_url, job_id, self.job_manager)

        if chat_path and chat_path.exists():
            self.chat_analyzer.load_from_file(chat_path)
            chat_signals = self.chat_analyzer.analyze(duration)
        else:
            # Use mock chat for testing
            print("Using mock chat data for analysis")
            mock_messages = create_mock_chat(duration)
            self.chat_analyzer.messages = mock_messages
            chat_signals = self.chat_analyzer.analyze(duration)

        return chat_signals

    async def _detect_candidate_moments(
        self,
        transcript,
        audio_signals: List,
        chat_signals: List,
        duration: float
    ) -> List[Dict]:
        """
        Detect initial candidate moments using smart_moments logic
        enhanced with chat signals.
        """
        # Use the existing smart_moments analyzer as base
        # But enhance with our multi-signal approach

        # Get word-level data from transcript
        words = []
        for seg in transcript.segments:
            for w in seg.words:
                words.append({
                    "word": w.word,
                    "clean": w.word.lower().strip(".,!?"),
                    "start": w.start,
                    "end": w.end,
                    "importance": 0
                })

        print(f"[DEBUG] Detecting moments: {len(words)} words, {duration:.1f}s duration")

        # Calculate word features (reuse smart_moments logic)
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from smart_moments import IMPORTANT_WORDS, REACTION_WORDS, QUESTION_WORDS, calculate_word_features
        calculate_word_features(words)

        # Build candidates with enhanced scoring
        candidates = []
        window_sizes = [6.0, 8.0, 10.0, 12.0, 16.0, 20.0, 30.0]

        for window_size in window_sizes:
            start = 0.0
            while start < duration:
                end = min(duration, start + window_size)

                selected = [w for w in words if w["end"] > start and w["start"] < end]

                if len(selected) >= 3:
                    text = " ".join(w["word"] for w in selected)

                    # Base scores
                    speech_seconds = sum(
                        max(0, min(w["end"], end) - max(w["start"], start))
                        for w in selected
                    )
                    density = speech_seconds / max(1, end - start)
                    density_score = min(20.0, density * 20)
                    importance_score = min(20.0, sum(w["importance"] for w in selected))
                    reactions = sum(1 for w in selected if w["clean"] in REACTION_WORDS)
                    questions = sum(1 for w in selected if w["clean"] in QUESTION_WORDS)
                    reaction_score = min(15.0, reactions * 5)
                    question_score = min(10.0, questions * 2.5)

                    # Audio energy score
                    audio_energy = 0
                    for a in audio_signals:
                        if a.timestamp >= start and a.timestamp < end:
                            audio_energy = max(audio_energy, a.strength)
                    audio_score = min(25.0, audio_energy * 0.5)

                    # Chat signal score
                    chat_score = 0
                    for c in chat_signals:
                        if c.timestamp >= start and c.timestamp < end:
                            chat_score = max(chat_score, c.strength)
                    chat_score = min(20.0, chat_score * 0.3)

                    total = min(100.0, round(
                        density_score + importance_score + reaction_score +
                        question_score + audio_score + chat_score, 2
                    ))

                    candidates.append({
                        "start": round(start, 2),
                        "end": round(end, 2),
                        "score": total,
                        "word_count": len(selected),
                        "reaction_count": reactions,
                        "question_count": questions,
                        "speech_density": round(density, 3),
                        "audio_energy": round(audio_score, 2),
                        "chat_score": round(chat_score, 2),
                        "text": text
                    })

                start += 2.0

        # Sort and select top non-overlapping
        candidates.sort(key=lambda x: x["score"], reverse=True)

        selected = []
        for cand in candidates:
            overlaps = False
            for existing in selected:
                if cand["start"] < existing["end"] and cand["end"] > existing["start"]:
                    overlaps = True
                    break
            if not overlaps:
                # Add recommended padding
                cand["recommended_start"] = max(0, cand["start"] - 2.0)
                cand["recommended_end"] = min(duration, cand["end"] + 2.0)
                selected.append(cand)
                if len(selected) >= 20:
                    break

        return selected

    def _expand_moments(
        self,
        raw_moments: List[Dict],
        transcript,
        audio_signals: List,
        chat_signals: List,
        duration: float
    ) -> List[ExpandedMoment]:
        """Expand raw moments with intelligent context."""
        expanded = []

        for moment in raw_moments:
            expanded_moment = self.context_analyzer.expand_moment(
                moment["recommended_start"],
                moment["recommended_end"],
                transcript,
                audio_signals,
                chat_signals,
                duration
            )
            # Attach original score data
            expanded_moment.moment_score_data = moment
            expanded.append(expanded_moment)

        # Merge overlapping expanded moments
        merged = merge_overlapping_moments(expanded, threshold=5.0)
        return merged

    def _convert_to_candidates(
        self,
        ranked_clips,
        metadata_map: Dict
    ) -> List[ClipCandidate]:
        """Convert ranked clips to ClipCandidate objects for job storage."""
        candidates = []

        for clip in ranked_clips:
            metadata = metadata_map.get(f"rank_{clip.rank}")

            candidate = ClipCandidate(
                id=f"rank_{clip.rank}",
                start_time=clip.moment.expanded_start,
                end_time=clip.moment.expanded_end,
                duration=clip.moment.duration,
                score=clip.final_score,
                signals={
                    "hook": clip.moment_score.hook_score,
                    "emotion": clip.moment_score.emotion_score,
                    "reaction": clip.moment_score.reaction_score,
                    "surprise": clip.moment_score.surprise_score,
                    "humor": clip.moment_score.humor_score,
                    "story": clip.moment_score.story_score,
                    "transcript": clip.moment_score.transcript_score,
                    "chat": clip.moment_score.chat_score,
                },
                hook=clip.moment.hook_text,
                context_summary=clip.moment.context_text[:200],
                payoff=clip.moment.payoff_text,
                reaction=clip.moment.reaction_text,
                story_quality=clip.story_analysis.overall_score,
                transcript_excerpt=clip.moment.hook_text + " " + clip.moment.payoff_text,
                suggested_edit_style=clip.tags[0] if clip.tags else "default",
                metadata={
                    "title": metadata.title if metadata else "",
                    "caption": metadata.caption if metadata else "",
                    "hashtags": metadata.hashtags if metadata else [],
                    "tags": clip.tags,
                    "review_priority": clip.review_priority,
                }
            )
            candidates.append(candidate)

        return candidates

    def _load_clip_history(self) -> List[Dict]:
        """Load clip history for cross-VOD deduplication."""
        import sys
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from clip_history import _load
        return _load()


async def run_vod_pipeline(job_id: str, job_manager: JobManager) -> RankingResult:
    """
    Entry point to run the full VOD pipeline.

    Args:
        job_id: Job ID to process
        job_manager: JobManager instance

    Returns:
        RankingResult with ranked clips
    """
    orchestrator = PipelineOrchestrator(job_manager)
    return await orchestrator.run_full_pipeline(job_id)


async def run_clip_pipeline(job_id: str, job_manager: JobManager) -> RankingResult:
    """
    Entry point to run the clip analysis pipeline.

    Args:
        job_id: Job ID to process
        job_manager: JobManager instance

    Returns:
        RankingResult with ranked clips
    """
    orchestrator = PipelineOrchestrator(job_manager)
    return await orchestrator.run_clip_pipeline(job_id)


async def run_live_pipeline(job_id: str, job_manager: JobManager) -> RankingResult:
    """
    Entry point to run the live stream analysis pipeline.

    Args:
        job_id: Job ID to process
        job_manager: JobManager instance

    Returns:
        RankingResult with ranked clips
    """
    orchestrator = PipelineOrchestrator(job_manager)
    return await orchestrator.run_live_pipeline(job_id)