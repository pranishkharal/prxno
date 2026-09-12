"""
Chat Analyzer - Analyzes KICK chat for viral moment signals.

Signals detected:
- Chat spikes (sudden message volume increase)
- Emote bursts
- Repeated phrases/names
- Audience reactions (questions, excitement)
- Hype moments

Note: KICK chat replay API may require authentication or may not be publicly available.
This module provides a flexible interface that can work with:
1. KICK official API (if available)
2. Chat replay files (JSON/CSV exports)
3. Third-party chat logs
4. Mock data for testing
"""

import asyncio
import json
import re
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any, Callable
import aiohttp

from .config import CONFIG, ChatConfig
from .job_manager import JobManager, JobState, JobErrorType


@dataclass
class ChatMessage:
    """A single chat message."""
    timestamp: float  # Seconds from VOD start
    username: str
    message: str
    emotes: List[str] = field(default_factory=list)
    badges: List[str] = field(default_factory=list)
    is_mod: bool = False
    is_subscriber: bool = False


@dataclass
class ChatWindow:
    """Aggregated chat statistics for a time window."""
    start_time: float
    end_time: float
    message_count: int
    unique_users: int
    emote_count: int
    top_emotes: List[Tuple[str, int]]
    top_words: List[Tuple[str, int]]
    repeated_phrases: List[Tuple[str, int]]
    questions_count: int
    exclamation_count: int
    caps_ratio: float
    spike_score: float = 0.0
    emote_burst_score: float = 0.0


@dataclass
class ChatSignal:
    """A detected chat signal at a specific time."""
    signal_type: str  # "spike", "emote_burst", "repeated_phrase", "reaction"
    timestamp: float
    strength: float  # 0-100
    details: Dict[str, Any]
    window: ChatWindow


class ChatAnalyzer:
    """
    Analyzes chat messages for viral moment signals.
    """

    def __init__(self, config: ChatConfig = None):
        self.config = config or CONFIG.chat
        self.messages: List[ChatMessage] = []
        self.windows: List[ChatWindow] = []
        self.signals: List[ChatSignal] = []
        self._baseline_msg_rate: float = 0.0

    def load_from_file(self, path: Path) -> int:
        """Load chat messages from JSON file."""
        self.messages = []
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        # Support multiple formats
        if isinstance(data, list):
            for item in data:
                self.messages.append(self._parse_message(item))
        elif isinstance(data, dict) and 'messages' in data:
            for item in data['messages']:
                self.messages.append(self._parse_message(item))

        self.messages.sort(key=lambda m: m.timestamp)
        return len(self.messages)

    def load_from_kick_api(self, vod_id: str, session_cookies: dict = None) -> int:
        """
        Load chat from KICK API (placeholder - implement when API is available).
        """
        # TODO: Implement KICK chat replay API integration
        # This would require:
        # 1. VOD ID extraction from URL
        # 2. Authenticated API request to KICK
        # 3. Parse chat replay format
        print("KICK chat API integration not yet implemented")
        return 0

    def _parse_message(self, item: dict) -> ChatMessage:
        """Parse a raw message dict into ChatMessage."""
        # Flexible parsing for different formats
        timestamp = float(item.get('timestamp', item.get('time', item.get('t', 0))))
        username = str(item.get('username', item.get('user', item.get('u', 'unknown'))))
        message = str(item.get('message', item.get('msg', item.get('m', ''))))
        emotes = item.get('emotes', item.get('e', []))
        if isinstance(emotes, str):
            emotes = [emotes]
        badges = item.get('badges', item.get('b', []))
        if isinstance(badges, str):
            badges = [badges]
        is_mod = bool(item.get('is_mod', item.get('mod', False)))
        is_sub = bool(item.get('is_subscriber', item.get('sub', False)))
        return ChatMessage(
            timestamp=timestamp,
            username=username,
            message=message,
            emotes=emotes,
            badges=badges,
            is_mod=is_mod,
            is_subscriber=is_sub
        )

    def analyze(self, duration: float) -> List[ChatSignal]:
        """
        Run full chat analysis on loaded messages.

        Args:
            duration: Total VOD duration in seconds

        Returns:
            List of detected chat signals
        """
        if not self.messages:
            return []

        self._build_windows(duration)
        self._calculate_baseline()
        self._detect_spikes()
        self._detect_emote_bursts()
        self._detect_repeated_phrases()
        self._detect_reactions()

        self.signals.sort(key=lambda s: s.timestamp)
        return self.signals

    def _build_windows(self, duration: float):
        """Build time windows across the VOD duration."""
        self.windows = []
        window_size = self.config.window_seconds
        step = window_size / 2  # 50% overlap

        current = 0.0
        while current < duration:
            end = min(current + window_size, duration)
            window = self._analyze_window(current, end)
            self.windows.append(window)
            current += step

    def _analyze_window(self, start: float, end: float) -> ChatWindow:
        """Analyze messages in a time window."""
        window_msgs = [m for m in self.messages if start <= m.timestamp < end]

        if not window_msgs:
            return ChatWindow(start, end, 0, 0, 0, [], [], [], 0, 0, 0.0)

        msg_count = len(window_msgs)
        unique_users = len(set(m.username for m in window_msgs))

        # Emotes
        all_emotes = []
        for m in window_msgs:
            all_emotes.extend(m.emotes)
        emote_count = len(all_emotes)
        top_emotes = Counter(all_emotes).most_common(5)

        # Word analysis
        all_words = []
        repeated_phrases = []
        questions = 0
        exclamations = 0
        caps_chars = 0
        total_chars = 0

        for m in window_msgs:
            msg = m.message
            total_chars += len(msg)
            caps_chars += sum(1 for c in msg if c.isupper())

            if '?' in msg:
                questions += 1
            if '!' in msg:
                exclamations += 1

            # Extract words (3+ chars)
            words = re.findall(r'\b\w{3,}\b', msg.lower())
            all_words.extend(words)

        top_words = Counter(all_words).most_common(10)

        # Find repeated phrases (3+ words appearing multiple times)
        phrases = []
        for m in window_msgs:
            # Extract 3-6 word phrases
            words = m.message.lower().split()
            for i in range(len(words) - 2):
                for length in range(3, min(7, len(words) - i + 1)):
                    phrase = ' '.join(words[i:i+length])
                    if len(phrase) > 10:
                        phrases.append(phrase)
        repeated_phrases = Counter(phrases).most_common(5)

        caps_ratio = caps_chars / max(1, total_chars)

        return ChatWindow(
            start_time=start,
            end_time=end,
            message_count=msg_count,
            unique_users=unique_users,
            emote_count=emote_count,
            top_emotes=top_emotes,
            top_words=top_words,
            repeated_phrases=repeated_phrases,
            questions_count=questions,
            exclamation_count=exclamations,
            caps_ratio=caps_ratio,
        )

    def _calculate_baseline(self):
        """Calculate baseline message rate for spike detection."""
        if not self.windows:
            self._baseline_msg_rate = 0.0
            return

        rates = [w.message_count / max(1, w.end_time - w.start_time) for w in self.windows]
        self._baseline_msg_rate = statistics.median(rates) if rates else 0.0

    def _detect_spikes(self):
        """Detect chat volume spikes."""
        if self._baseline_msg_rate <= 0:
            return

        threshold = self._baseline_msg_rate * self.config.spike_threshold_multiplier
        min_messages = self.config.min_messages_for_spike

        for window in self.windows:
            rate = window.message_count / max(1, window.end_time - window.start_time)
            if window.message_count >= min_messages and rate > threshold:
                strength = min(100, (rate / max(0.01, self._baseline_msg_rate)) * 20)
                self.signals.append(ChatSignal(
                    signal_type="spike",
                    timestamp=(window.start_time + window.end_time) / 2,
                    strength=strength,
                    details={
                        "message_count": window.message_count,
                        "rate": round(rate, 2),
                        "baseline": round(self._baseline_msg_rate, 2),
                        "unique_users": window.unique_users,
                        "top_words": window.top_words[:3],
                    },
                    window=window,
                ))

    def _detect_emote_bursts(self):
        """Detect emote usage bursts."""
        for window in self.windows:
            if window.emote_count >= self.config.emote_burst_threshold:
                strength = min(100, window.emote_count * 2)
                self.signals.append(ChatSignal(
                    signal_type="emote_burst",
                    timestamp=(window.start_time + window.end_time) / 2,
                    strength=strength,
                    details={
                        "emote_count": window.emote_count,
                        "top_emotes": window.top_emotes[:3],
                        "unique_users": window.unique_users,
                    },
                    window=window,
                ))

    def _detect_repeated_phrases(self):
        """Detect phrases repeated by multiple users."""
        for window in self.windows:
            for phrase, count in window.repeated_phrases:
                if count >= 3:  # At least 3 people said similar thing
                    strength = min(100, count * 8)
                    self.signals.append(ChatSignal(
                        signal_type="repeated_phrase",
                        timestamp=(window.start_time + window.end_time) / 2,
                        strength=strength,
                        details={
                            "phrase": phrase,
                            "count": count,
                            "top_words": window.top_words[:3],
                        },
                        window=window,
                    ))

    def _detect_reactions(self):
        """Detect audience reaction patterns."""
        for window in self.windows:
            # High question rate = confusion/curiosity
            if window.questions_count >= 3:
                rate = window.questions_count / max(1, window.message_count)
                if rate > 0.15:
                    self.signals.append(ChatSignal(
                        signal_type="reaction_confusion",
                        timestamp=(window.start_time + window.end_time) / 2,
                        strength=min(100, rate * 200),
                        details={
                            "questions": window.questions_count,
                            "question_rate": round(rate, 2),
                        },
                        window=window,
                    ))

            # High exclamation rate = excitement
            if window.exclamation_count >= 5:
                rate = window.exclamation_count / max(1, window.message_count)
                if rate > 0.2:
                    self.signals.append(ChatSignal(
                        signal_type="reaction_excitement",
                        timestamp=(window.start_time + window.end_time) / 2,
                        strength=min(100, rate * 150),
                        details={
                            "exclamations": window.exclamation_count,
                            "exclamation_rate": round(rate, 2),
                            "caps_ratio": round(window.caps_ratio, 2),
                        },
                        window=window,
                    ))

    def get_signal_at_time(self, timestamp: float, radius: float = 15.0) -> List[ChatSignal]:
        """Get all signals near a timestamp."""
        return [
            s for s in self.signals
            if abs(s.timestamp - timestamp) <= radius
        ]

    def get_aggregate_score(self, timestamp: float, radius: float = 15.0) -> float:
        """Get aggregate chat signal score near a timestamp."""
        signals = self.get_signal_at_time(timestamp, radius)
        if not signals:
            return 0.0
        # Weight by recency and strength
        total = 0.0
        for s in signals:
            distance_factor = 1.0 - (abs(s.timestamp - timestamp) / radius)
            total += s.strength * distance_factor
        return min(100.0, total / len(signals))


async def fetch_kick_chat_replay(vod_url: str, job_id: str, job_manager: JobManager) -> Optional[Path]:
    """
    Attempt to fetch KICK chat replay for a VOD.

    This is a placeholder - KICK's chat replay API may require:
    - Authentication
    - Specific headers
    - Different endpoint

    Returns path to saved chat JSON file or None if unavailable.
    """
    # Extract VOD ID from URL
    vod_id = extract_vod_id(vod_url)
    if not vod_id:
        return None

    job_manager.update_progress(job_id, 10, "Fetching chat replay...")

    # Try KICK API endpoints (these are speculative)
    endpoints = [
        f"https://kick.com/api/v2/videos/{vod_id}/chat",
        f"https://kick.com/api/v1/videos/{vod_id}/chat",
        f"https://kick.com/{vod_id}/chat/replay",
    ]

    async with aiohttp.ClientSession() as session:
        for endpoint in endpoints:
            try:
                async with session.get(endpoint, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        # Save to file
                        chat_path = CONFIG.paths.analysis_cache_dir / f"chat_{job_id}.json"
                        with open(chat_path, 'w') as f:
                            json.dump(data, f)
                        job_manager.update_progress(job_id, 100, "Chat replay fetched")
                        return chat_path
            except Exception as e:
                print(f"Chat fetch failed for {endpoint}: {e}")
                continue

    job_manager.update_progress(job_id, 100, "Chat replay unavailable")
    return None


def extract_vod_id(url: str) -> Optional[str]:
    """Extract VOD ID from KICK URL."""
    try:
        from urllib.parse import urlparse
        parsed = urlparse(url.strip())
        path = (parsed.path or "").strip("/")
        segments = [s for s in path.split("/") if s]
        # Look for numeric ID at the end
        for seg in reversed(segments):
            if seg.isdigit():
                return seg
    except Exception:
        pass
    return None


def create_mock_chat(vod_duration: float, seed: int = 42) -> List[ChatMessage]:
    """Create mock chat data for testing (deterministic)."""
    import random
    random.seed(seed)

    messages = []
    emotes = ["KEKW", "OMEGALUL", "POG", "POGGERS", "monkaS", "Pepega", "FeelsBadMan", "FeelsGoodMan"]
    phrases = [
        "this is crazy", "no way", "bro what", "wait what", "omg",
        "clip it", "clippable", "this is content", "chat reacted",
        "streamer reacted", "funny moment", "insane", "unbelievable"
    ]

    # Base activity
    for t in range(0, int(vod_duration), 5):
        count = random.randint(1, 8)
        for _ in range(count):
            messages.append(ChatMessage(
                timestamp=t + random.random() * 5,
                username=f"user{random.randint(1, 500)}",
                message=random.choice(phrases),
                emotes=[random.choice(emotes)] if random.random() < 0.3 else [],
            ))

    # Add spikes at specific moments
    spike_times = [vod_duration * 0.2, vod_duration * 0.5, vod_duration * 0.8]
    for spike_t in spike_times:
        for _ in range(random.randint(30, 80)):
            messages.append(ChatMessage(
                timestamp=spike_t + random.random() * 10,
                username=f"user{random.randint(1, 500)}",
                message=random.choice(["CLIP IT", "CLIPPPP", "OMG CLIP", "THIS IS CONTENT"]),
                emotes=[random.choice(emotes)] if random.random() < 0.7 else [],
            ))

    messages.sort(key=lambda m: m.timestamp)
    return messages