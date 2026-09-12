"""
Configuration for VOD Intelligence Pipeline.

All scoring weights, thresholds, and model settings are centralized here
for easy tuning without code changes.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List
from pathlib import Path


class ContentType(Enum):
    """Type of content being analyzed."""
    VOD = "vod"           # Full VOD (hours)
    CLIP = "clip"         # Short clip (10-180s)
    LIVE = "live"         # Live stream (real-time)


@dataclass
class ContentTypeConfig:
    """Configuration per content type."""
    # Analysis windows
    analysis_window_seconds: float = 30.0
    overlap_seconds: float = 10.0
    
    # Minimum/maximum clip durations
    min_clip_duration: float = 5.0
    max_clip_duration: float = 120.0
    
    # Scoring adjustments
    hook_weight_multiplier: float = 1.0
    context_weight_multiplier: float = 1.0
    enable_chat_analysis: bool = True
    enable_live_buffering: bool = False
    
    # Processing
    fast_mode: bool = False  # Skip expensive LLM analysis
    buffer_duration_seconds: float = 60.0  # For live


# Preset configurations per content type
CONTENT_TYPE_CONFIGS = {
    ContentType.VOD: ContentTypeConfig(
        analysis_window_seconds=30.0,
        overlap_seconds=10.0,
        min_clip_duration=8.0,
        max_clip_duration=120.0,
        hook_weight_multiplier=1.0,
        context_weight_multiplier=1.0,
        enable_chat_analysis=True,
        enable_live_buffering=False,
        fast_mode=False,
        buffer_duration_seconds=0,
    ),
    ContentType.CLIP: ContentTypeConfig(
        analysis_window_seconds=10.0,
        overlap_seconds=5.0,
        min_clip_duration=3.0,
        max_clip_duration=180.0,
        hook_weight_multiplier=1.5,  # Hook more important for short clips
        context_weight_multiplier=0.5,  # Less context needed
        enable_chat_analysis=False,  # Clips don't have chat replay usually
        enable_live_buffering=False,
        fast_mode=True,  # Skip LLM for speed
        buffer_duration_seconds=0,
    ),
    ContentType.LIVE: ContentTypeConfig(
        analysis_window_seconds=15.0,
        overlap_seconds=5.0,
        min_clip_duration=5.0,
        max_clip_duration=90.0,
        hook_weight_multiplier=1.2,
        context_weight_multiplier=0.8,
        enable_chat_analysis=True,  # Live chat available
        enable_live_buffering=True,
        fast_mode=True,
        buffer_duration_seconds=60.0,
    ),
}


@dataclass
class ScoringWeights:
    """Weights for moment scoring components. Must sum to 1.0 for normalized output."""
    hook: float = 0.20
    emotional_intensity: float = 0.15
    reaction: float = 0.15
    surprise: float = 0.10
    humor: float = 0.10
    story_value: float = 0.10
    transcript_relevance: float = 0.08
    chat_reaction: float = 0.07
    novelty: float = 0.03
    pacing: float = 0.02

    def __post_init__(self):
        total = sum(v for k, v in self.__dict__.items() if not k.startswith('_'))
        if abs(total - 1.0) > 0.01:
            raise ValueError(f"Scoring weights must sum to 1.0, got {total}")


@dataclass
class AudioConfig:
    """Audio analysis configuration."""
    window_seconds: float = 0.1
    baseline_percentile: float = 50.0
    energy_multiplier: float = 3.0
    max_energy_score: float = 25.0
    laughter_detection_enabled: bool = True
    silence_threshold_db: float = -40.0
    min_speech_duration: float = 0.5


@dataclass
class TranscriptConfig:
    """Transcription and semantic analysis configuration."""
    model_size: str = "base"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 5
    vad_filter: bool = True
    language: str = "en"
    llm_model: str = "llama3.2"
    llm_temperature: float = 0.3
    max_context_chars: int = 4000


@dataclass
class ChatConfig:
    """Chat analysis configuration."""
    enabled: bool = True
    spike_threshold_multiplier: float = 3.0
    emote_burst_threshold: int = 10
    window_seconds: float = 30.0
    min_messages_for_spike: int = 5


@dataclass
class VisualConfig:
    """Visual analysis configuration (optional, lightweight)."""
    enabled: bool = False
    scene_change_threshold: float = 0.3
    face_detection_enabled: bool = False
    sample_interval_seconds: float = 5.0


@dataclass
class ContextConfig:
    """Context expansion configuration."""
    pre_context_seconds: float = 8.0
    post_context_seconds: float = 5.0
    max_total_duration: float = 120.0
    min_context_duration: float = 3.0
    preserve_meaningful_pauses: bool = True
    pause_significance_threshold: float = 1.5


@dataclass
class StoryConfig:
    """Story structure analysis configuration."""
    hook_weight: float = 0.30
    context_weight: float = 0.20
    payoff_weight: float = 0.30
    reaction_weight: float = 0.20
    min_hook_score: float = 20.0
    min_payoff_score: float = 15.0


@dataclass
class RankingConfig:
    """Final clip ranking configuration."""
    max_candidates: int = 20
    top_n_for_review: int = 5
    duplicate_time_threshold: float = 15.0
    duplicate_text_similarity: float = 0.6
    story_quality_weight: float = 0.35
    hook_weight: float = 0.20
    payoff_weight: float = 0.15
    emotional_weight: float = 0.10
    context_weight: float = 0.10
    novelty_weight: float = 0.05
    pacing_weight: float = 0.05


@dataclass
class EditConfig:
    """Content-aware editing configuration."""
    enable_smart_captions: bool = True
    enable_dead_space_removal: bool = True
    max_silence_removal_ratio: float = 0.4
    preserve_pauses_above: float = 1.0
    caption_style: str = "word_by_word"
    default_preset: str = "TikTok"


@dataclass
class JobConfig:
    """Job processing configuration."""
    max_concurrent_jobs: int = 2
    max_concurrent_analysis: int = 3
    download_timeout_seconds: int = 3600
    analysis_timeout_seconds: int = 7200
    edit_timeout_seconds: int = 1800
    cleanup_after_hours: int = 24
    max_vod_duration_hours: float = 12.0
    min_vod_duration_seconds: int = 60


@dataclass
class Paths:
    """Filesystem paths."""
    base_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent)
    jobs_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent / "vod_jobs")
    downloads_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent / "vod_downloads")
    analysis_cache_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent / "vod_analysis_cache")
    output_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent / "output")
    temp_dir: Path = field(default_factory=lambda: Path(__file__).parent.parent / "temp")

    def __post_init__(self):
        for path in [self.jobs_dir, self.downloads_dir, self.analysis_cache_dir, self.output_dir, self.temp_dir]:
            path.mkdir(parents=True, exist_ok=True)


@dataclass
class VODIntelligenceConfig:
    """Master configuration container."""
    scoring: ScoringWeights = field(default_factory=ScoringWeights)
    audio: AudioConfig = field(default_factory=AudioConfig)
    transcript: TranscriptConfig = field(default_factory=TranscriptConfig)
    chat: ChatConfig = field(default_factory=ChatConfig)
    visual: VisualConfig = field(default_factory=VisualConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    story: StoryConfig = field(default_factory=StoryConfig)
    ranking: RankingConfig = field(default_factory=RankingConfig)
    edit: EditConfig = field(default_factory=EditConfig)
    job: JobConfig = field(default_factory=JobConfig)
    paths: Paths = field(default_factory=Paths)

    # Signal enable/disable flags
    enable_audio: bool = True
    enable_transcript: bool = True
    enable_chat: bool = True
    enable_visual: bool = False
    enable_llm_analysis: bool = True

    @classmethod
    def load(cls, config_path: Path = None) -> "VODIntelligenceConfig":
        """Load config from YAML file if exists, otherwise return defaults."""
        if config_path and config_path.exists():
            import yaml
            with open(config_path, 'r') as f:
                data = yaml.safe_load(f)
            # TODO: Implement proper nested dataclass loading
        return cls()


# Global config instance
CONFIG = VODIntelligenceConfig()


# Signal names for consistent reference
SIGNAL_NAMES = [
    "audio_energy",
    "audio_laughter",
    "audio_energy_shift",
    "transcript_keywords",
    "transcript_emotion",
    "transcript_storytelling",
    "transcript_questions",
    "transcript_controversy",
    "chat_spike",
    "chat_emote_burst",
    "chat_repeated_phrases",
    "visual_scene_change",
    "visual_face_reaction",
]

DEFAULT_SIGNAL_WEIGHTS = {
    "audio_energy": 0.12,
    "audio_laughter": 0.10,
    "audio_energy_shift": 0.08,
    "transcript_keywords": 0.15,
    "transcript_emotion": 0.12,
    "transcript_storytelling": 0.10,
    "transcript_questions": 0.05,
    "transcript_controversy": 0.08,
    "chat_spike": 0.10,
    "chat_emote_burst": 0.05,
    "chat_repeated_phrases": 0.05,
    "visual_scene_change": 0.03,
    "visual_face_reaction": 0.02,
}