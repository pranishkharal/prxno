"""
VOD Intelligence Package - AI-assisted stream content intelligence + short-form editing platform.
"""

from .config import CONFIG, VODIntelligenceConfig, ScoringWeights, ContentType, CONTENT_TYPE_CONFIGS
from .job_manager import JobManager, VODJob, JobState, JobErrorType, ClipCandidate, get_job_manager
from .vod_downloader import download_kick_vod, probe_media, is_kick_vod_url, extract_streamer_from_vod_url
from .chat_analyzer import ChatAnalyzer, ChatMessage, ChatSignal, fetch_kick_chat_replay
from .audio_analyzer import AudioAnalyzer, AudioSignal
from .transcript_analyzer import TranscriptAnalyzer, TranscriptAnalysis
from .context_analyzer import ContextAnalyzer, ExpandedMoment
from .moment_scorer import MomentScorer, MomentScore, score_all_moments
from .story_analyzer import StoryAnalyzer, StoryAnalysis, analyze_all_stories
from .clip_ranker import ClipRanker, RankingResult, RankedClip
from .edit_planner import EditPlanner, EditPlan
from .metadata_generator import MetadataGenerator, ClipMetadata, generate_metadata_for_all_clips
from .review_interface import ClipReviewView, ClipDetailView, start_review_session
from .pipeline import PipelineOrchestrator, run_vod_pipeline, run_clip_pipeline, run_live_pipeline
from .caption_engine import CaptionEngine, OnScreenCaptionGenerator, PostCaptionGenerator, CaptionSegment, PostCaptionResult, detect_emotion_category

__all__ = [
    # Config
    "CONFIG",
    "VODIntelligenceConfig",
    "ScoringWeights",
    "ContentType",
    "CONTENT_TYPE_CONFIGS",

    # Job Management
    "JobManager",
    "VODJob",
    "JobState",
    "JobErrorType",
    "ClipCandidate",
    "get_job_manager",

    # Downloader
    "download_kick_vod",
    "probe_media",
    "is_kick_vod_url",
    "extract_streamer_from_vod_url",

    # Analyzers
    "ChatAnalyzer",
    "ChatMessage",
    "ChatSignal",
    "fetch_kick_chat_replay",
    "AudioAnalyzer",
    "AudioSignal",
    "TranscriptAnalyzer",
    "TranscriptAnalysis",
    "ContextAnalyzer",
    "ExpandedMoment",

    # Intelligence
    "MomentScorer",
    "MomentScore",
    "score_all_moments",
    "StoryAnalyzer",
    "StoryAnalysis",
    "analyze_all_stories",
    "ClipRanker",
    "RankingResult",
    "RankedClip",
    "EditPlanner",
    "EditPlan",
    "MetadataGenerator",
    "ClipMetadata",
    "generate_metadata_for_all_clips",

    # Interface
    "ClipReviewView",
    "ClipDetailView",
    "start_review_session",

    # Pipeline
    "PipelineOrchestrator",
    "run_vod_pipeline",
    "run_clip_pipeline",
    "run_live_pipeline",

    # Caption Engine
    "CaptionEngine",
    "OnScreenCaptionGenerator",
    "PostCaptionGenerator",
    "CaptionSegment",
    "PostCaptionResult",
    "detect_emotion_category",
]

__version__ = "1.0.0"