"""
Transcript Analyzer - Semantic understanding of transcribed content.

Uses faster-whisper for transcription, then LLM (Ollama) for deep analysis:
- Topic detection
- Emotion classification
- Storytelling structure
- Controversy/surprise detection
- Question/argument identification
- Named entity recognition (names, games, events)
- Hook identification
- Payoff detection
"""

import asyncio
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from faster_whisper import WhisperModel

from .config import CONFIG, TranscriptConfig
from .job_manager import JobManager, JobState, JobErrorType


@dataclass
class TranscriptWord:
    """A single word with timing."""
    word: str
    start: float
    end: float
    confidence: float = 1.0


@dataclass
class TranscriptSegment:
    """A transcript segment (sentence/phrase)."""
    text: str
    start: float
    end: float
    words: List[TranscriptWord]
    speaker: Optional[str] = None


@dataclass
class TranscriptAnalysis:
    """Complete transcript analysis results."""
    full_text: str
    segments: List[TranscriptSegment]
    duration: float
    word_count: int
    language: str
    topics: List[Dict[str, Any]]
    emotions: List[Dict[str, Any]]
    storytelling_score: float
    hook_candidates: List[Dict[str, Any]]
    payoff_candidates: List[Dict[str, Any]]
    controversy_score: float
    surprise_score: float
    named_entities: List[Dict[str, Any]]
    questions: List[Dict[str, Any]]
    arguments: List[Dict[str, Any]]
    key_phrases: List[str]


class TranscriptAnalyzer:
    """
    Transcribes and semantically analyzes video content.
    """

    def __init__(self, config: TranscriptConfig = None):
        self.config = config or CONFIG.transcript
        self._model: Optional[WhisperModel] = None
        self._ollama_available = False

    def _get_model(self) -> WhisperModel:
        """Lazy load Whisper model."""
        if self._model is None:
            print(f"Loading Whisper model ({self.config.model_size})...")
            self._model = WhisperModel(
                self.config.model_size,
                device=self.config.device,
                compute_type=self.config.compute_type
            )
            print("Whisper model loaded.")
        return self._model

    async def transcribe(self, video_path: Path, job_id: str, job_manager: JobManager,
                        progress_callback: Optional[callable] = None,
                        fast_mode: bool = False) -> TranscriptAnalysis:
        """
        Transcribe video and run semantic analysis.

        Args:
            video_path: Path to video file
            job_id: Job ID for tracking
            job_manager: Job manager for state updates
            progress_callback: Optional callback(progress, step)
            fast_mode: Skip LLM semantic analysis for speed

        Returns:
            TranscriptAnalysis with full results
        """
        job_manager.update_state(job_id, JobState.TRANSCRIBING, 0, "Transcribing with Whisper...")

        # Run transcription
        segments, info = await self._transcribe_video(video_path, job_id, job_manager, progress_callback)

        if not segments:
            return TranscriptAnalysis(
                full_text="",
                segments=[],
                duration=0,
                word_count=0,
                language=info.language if info else "en",
                topics=[],
                emotions=[],
                storytelling_score=0,
                hook_candidates=[],
                payoff_candidates=[],
                controversy_score=0,
                surprise_score=0,
                named_entities=[],
                questions=[],
                arguments=[],
                key_phrases=[],
            )

        # Build transcript segments
        transcript_segments = self._build_segments(segments)

        job_manager.update_state(job_id, JobState.TRANSCRIBING, 80, "Running semantic analysis...")

        # Skip LLM analysis in fast mode
        if fast_mode:
            analysis = TranscriptAnalysis(
                full_text=" ".join(s.text for s in transcript_segments),
                segments=transcript_segments,
                duration=transcript_segments[-1].end if transcript_segments else 0,
                word_count=sum(len(s.words) for s in transcript_segments),
                language=info.language if info else "en",
                topics=[],
                emotions=[],
                storytelling_score=0,
                hook_candidates=[],
                payoff_candidates=[],
                controversy_score=0,
                surprise_score=0,
                named_entities=[],
                questions=[],
                arguments=[],
                key_phrases=[],
            )
        else:
            analysis = await self._analyze_semantics(transcript_segments, job_id, job_manager)

        job_manager.update_state(job_id, JobState.TRANSCRIBING, 100, "Transcription complete")

        return analysis

    async def _transcribe_video(self, video_path: Path, job_id: str, job_manager: JobManager,
                               progress_callback: Optional[callable]) -> Tuple[List, Any]:
        """Run Whisper transcription in thread pool."""
        model = self._get_model()

        def _transcribe():
            segments, info = model.transcribe(
                str(video_path),
                beam_size=self.config.beam_size,
                word_timestamps=True,
                vad_filter=self.config.vad_filter,
                language=self.config.language if self.config.language != "auto" else None
            )
            return list(segments), info

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _transcribe)

    def _build_segments(self, segments: List) -> List[TranscriptSegment]:
        """Convert Whisper segments to TranscriptSegments."""
        result = []
        for seg in segments:
            words = []
            if hasattr(seg, 'words') and seg.words:
                for w in seg.words:
                    words.append(TranscriptWord(
                        word=w.word.strip(),
                        start=float(w.start),
                        end=float(w.end),
                        confidence=getattr(w, 'probability', 1.0)
                    ))
            result.append(TranscriptSegment(
                text=seg.text.strip(),
                start=float(seg.start),
                end=float(seg.end),
                words=words
            ))
        return result

    async def _analyze_semantics(self, segments: List[TranscriptSegment],
                                job_id: str, job_manager: JobManager) -> TranscriptAnalysis:
        """Run LLM-based semantic analysis on transcript."""
        full_text = " ".join(s.text for s in segments)
        duration = segments[-1].end if segments else 0
        word_count = sum(len(s.words) for s in segments)

        # Skip LLM analysis if Ollama is not available
        if not self._check_ollama():
            print("Ollama not available - skipping semantic analysis")
            return TranscriptAnalysis(
                full_text=full_text,
                segments=segments,
                duration=duration,
                word_count=word_count,
                language="en",
                topics=[],
                emotions=[],
                storytelling_score=0,
                hook_candidates=[],
                payoff_candidates=[],
                controversy_score=0,
                surprise_score=0,
                named_entities=[],
                questions=[],
                arguments=[],
                key_phrases=[],
            )

        # Run multiple analysis prompts in parallel
        tasks = [
            self._analyze_topics(full_text),
            self._analyze_emotions(full_text),
            self._analyze_storytelling(full_text, segments),
            self._analyze_hooks(full_text, segments),
            self._analyze_payoffs(full_text, segments),
            self._analyze_controversy_surprise(full_text),
            self._extract_entities(full_text),
            self._find_questions_arguments(full_text, segments),
            self._extract_key_phrases(full_text),
        ]

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Handle any exceptions
        processed = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                print(f"Analysis task {i} failed: {r}")
                processed.append({})
            else:
                processed.append(r)

        return TranscriptAnalysis(
            full_text=full_text,
            segments=segments,
            duration=duration,
            word_count=word_count,
            language="en",
            topics=processed[0].get("topics", []) if isinstance(processed[0], dict) else [],
            emotions=processed[1].get("emotions", []) if isinstance(processed[1], dict) else [],
            storytelling_score=processed[2].get("score", 0) if isinstance(processed[2], dict) else 0,
            hook_candidates=processed[3].get("hooks", []) if isinstance(processed[3], dict) else [],
            payoff_candidates=processed[4].get("payoffs", []) if isinstance(processed[4], dict) else [],
            controversy_score=processed[5].get("controversy", 0) if isinstance(processed[5], dict) else 0,
            surprise_score=processed[5].get("surprise", 0) if isinstance(processed[5], dict) else 0,
            named_entities=processed[6].get("entities", []) if isinstance(processed[6], dict) else [],
            questions=processed[7].get("questions", []) if isinstance(processed[7], dict) else [],
            arguments=processed[7].get("arguments", []) if isinstance(processed[7], dict) else [],
            key_phrases=processed[8].get("phrases", []) if isinstance(processed[8], dict) else [],
        )

    async def _ollama_query(self, prompt: str, system: str = "") -> Optional[dict]:
        """Query Ollama for analysis."""
        if not self._check_ollama():
            return None

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                payload = {
                    "model": self.config.llm_model,
                    "prompt": prompt,
                    "system": system,
                    "stream": False,
                    "options": {"temperature": self.config.llm_temperature}
                }
                async with session.post(
                    "http://localhost:11434/api/generate",
                    json=payload,
                    timeout=120
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return json.loads(data.get("response", "{}"))
        except Exception as e:
            print(f"Ollama query failed: {e}")
        return None

    def _check_ollama(self) -> bool:
        """Check if Ollama is available."""
        try:
            import requests
            resp = requests.get("http://localhost:11434", timeout=2)
            self._ollama_available = resp.status_code < 500
        except Exception:
            self._ollama_available = False
        return self._ollama_available

    async def _analyze_topics(self, text: str) -> dict:
        """Identify main topics in the transcript."""
        prompt = f"""Analyze this streamer transcript and identify the main topics discussed.
Return JSON with: {{"topics": [{{"topic": "string", "relevance": 0-100, "time_range": [start, end]}}]}}

Transcript (first {self.config.max_context_chars} chars):
{text[:self.config.max_context_chars]}"""
        result = await self._ollama_query(prompt, "You are a content analyst for streamer videos.")
        return result or {"topics": []}

    async def _analyze_emotions(self, text: str) -> dict:
        """Classify emotional content."""
        prompt = f"""Analyze the emotional tone of this streamer transcript.
Return JSON with: {{"emotions": [{{"emotion": "string", "intensity": 0-100, "evidence": "quote"}}]}}

Emotions to detect: excitement, anger, surprise, joy, sadness, fear, confusion, anticipation, hype, frustration, wholesome, awkward

Transcript:
{text[:self.config.max_context_chars]}"""
        result = await self._ollama_query(prompt, "You are an emotion detection specialist for streamer content.")
        return result or {"emotions": []}

    async def _analyze_storytelling(self, text: str, segments: List[TranscriptSegment]) -> dict:
        """Analyze storytelling structure."""
        prompt = f"""Analyze the storytelling quality of this streamer transcript.
Return JSON with: {{"score": 0-100, "has_narrative_arc": true/false, "setup_payoff": true/false, "pacing": "fast/medium/slow", "notes": "string"}}

Look for: narrative arc, setup/payoff, character moments, escalating tension, resolution, callbacks

Transcript:
{text[:self.config.max_context_chars]}"""
        result = await self._ollama_query(prompt, "You are a storytelling analyst for viral short-form content.")
        return result or {"score": 0}

    async def _analyze_hooks(self, text: str, segments: List[TranscriptSegment]) -> dict:
        """Identify potential hooks (first 3 seconds of a clip)."""
        # Analyze first 30 seconds of each potential clip window
        prompt = f"""Identify the best HOOKS in this transcript - moments that grab attention in the first 3 seconds.
Return JSON with: {{"hooks": [{{"timestamp": float, "text": "string", "hook_type": "statement/question/reaction/surprise", "strength": 0-100, "why": "string"}}]}}

A good hook: unexpected statement, strong reaction, controversial claim, question, dramatic reveal, funny setup

Transcript with timestamps:
{self._format_with_timestamps(segments[:100])}"""
        result = await self._ollama_query(prompt, "You are a viral hook expert for TikTok/Reels/Shorts.")
        return result or {"hooks": []}

    async def _analyze_payoffs(self, text: str, segments: List[TranscriptSegment]) -> dict:
        """Identify payoffs (satisfying conclusions)."""
        prompt = f"""Identify PAYOFFS in this transcript - satisfying conclusions, punchlines, reveals, reactions.
Return JSON with: {{"payoffs": [{{"timestamp": float, "text": "string", "payoff_type": "punchline/reveal/reaction/resolution", "satisfaction": 0-100, "setup_timestamp": float}}]}}

A good payoff: delivers on a setup, surprising conclusion, emotional resolution, funny punchline

Transcript with timestamps:
{self._format_with_timestamps(segments)}"""
        result = await self._ollama_query(prompt, "You are a payoff analyst for short-form video satisfaction.")
        return result or {"payoffs": []}

    async def _analyze_controversy_surprise(self, text: str) -> dict:
        """Detect controversy and surprise elements."""
        prompt = f"""Analyze this transcript for CONTROVERSY and SURPRISE.
Return JSON with: {{"controversy": 0-100, "surprise": 0-100, "controversial_moments": ["quote"], "surprising_moments": ["quote"]}}

Controversy: divisive opinions, hot takes, drama, accusations, conflicts
Surprise: unexpected outcomes, reveals, plot twists, "I didn't see that coming"

Transcript:
{text[:self.config.max_context_chars]}"""
        result = await self._ollama_query(prompt, "You are a controversy and surprise detector for streamer clips.")
        return result or {"controversy": 0, "surprise": 0}

    async def _extract_entities(self, text: str) -> dict:
        """Extract named entities (people, games, events, organizations)."""
        prompt = f"""Extract NAMED ENTITIES from this streamer transcript.
Return JSON with: {{"entities": [{{"entity": "string", "type": "person/game/event/org/brand", "mentions": int, "context": "quote"}}]}}

Types: person (streamers, celebrities), game, event, organization, brand, meme

Transcript:
{text[:self.config.max_context_chars]}"""
        result = await self._ollama_query(prompt, "You are an entity extraction specialist for gaming/streaming content.")
        return result or {"entities": []}

    async def _find_questions_arguments(self, text: str, segments: List[TranscriptSegment]) -> dict:
        """Find questions and arguments/debates."""
        # Also do regex-based detection for reliability
        questions = []
        arguments = []

        for seg in segments:
            if '?' in seg.text:
                questions.append({
                    "timestamp": seg.start,
                    "text": seg.text,
                    "type": "direct_question"
                })
            # Detect argument patterns
            argument_indicators = ["you're wrong", "that's not true", "actually", "fact is", "prove it", "debate"]
            if any(ind in seg.text.lower() for ind in argument_indicators):
                arguments.append({
                    "timestamp": seg.start,
                    "text": seg.text,
                    "type": "argument"
                })

        return {"questions": questions, "arguments": arguments}

    async def _extract_key_phrases(self, text: str) -> dict:
        """Extract memorable/quotable phrases."""
        prompt = f"""Extract the most MEMORABLE, QUOTABLE, or CLIP-WORTHY phrases from this transcript.
Return JSON with: {{"phrases": ["phrase1", "phrase2", ...]}}

Look for: one-liners, catchphrases, emotional statements, funny lines, controversial takes, wisdom

Transcript:
{text[:self.config.max_context_chars]}"""
        result = await self._ollama_query(prompt, "You are a quotable moment extractor for viral clips.")
        return result or {"phrases": []}

    def _format_with_timestamps(self, segments: List[TranscriptSegment], max_segments: int = 200) -> str:
        """Format segments with timestamps for LLM."""
        lines = []
        for seg in segments[:max_segments]:
            lines.append(f"[{seg.start:.1f}s] {seg.text}")
        return "\n".join(lines)

    def save_analysis(self, analysis: TranscriptAnalysis, output_path: Path):
        """Save analysis to JSON file."""
        import dataclasses
        data = {
            "full_text": analysis.full_text,
            "duration": analysis.duration,
            "word_count": analysis.word_count,
            "language": analysis.language,
            "topics": analysis.topics,
            "emotions": analysis.emotions,
            "storytelling_score": analysis.storytelling_score,
            "hook_candidates": analysis.hook_candidates,
            "payoff_candidates": analysis.payoff_candidates,
            "controversy_score": analysis.controversy_score,
            "surprise_score": analysis.surprise_score,
            "named_entities": analysis.named_entities,
            "questions": analysis.questions,
            "arguments": analysis.arguments,
            "key_phrases": analysis.key_phrases,
            "segments": [
                {
                    "text": s.text,
                    "start": s.start,
                    "end": s.end,
                    "words": [{"word": w.word, "start": w.start, "end": w.end} for w in s.words]
                }
                for s in analysis.segments
            ]
        }
        output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')

    def load_analysis(self, path: Path) -> TranscriptAnalysis:
        """Load analysis from JSON file."""
        data = json.loads(path.read_text(encoding='utf-8'))
        segments = [
            TranscriptSegment(
                text=s["text"],
                start=s["start"],
                end=s["end"],
                words=[TranscriptWord(**w) for w in s.get("words", [])]
            )
            for s in data.get("segments", [])
        ]
        return TranscriptAnalysis(
            full_text=data["full_text"],
            segments=segments,
            duration=data["duration"],
            word_count=data["word_count"],
            language=data["language"],
            topics=data.get("topics", []),
            emotions=data.get("emotions", []),
            storytelling_score=data.get("storytelling_score", 0),
            hook_candidates=data.get("hook_candidates", []),
            payoff_candidates=data.get("payoff_candidates", []),
            controversy_score=data.get("controversy_score", 0),
            surprise_score=data.get("surprise_score", 0),
            named_entities=data.get("named_entities", []),
            questions=data.get("questions", []),
            arguments=data.get("arguments", []),
            key_phrases=data.get("key_phrases", []),
        )