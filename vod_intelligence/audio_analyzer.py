"""
Audio Analyzer - Enhanced audio analysis for viral moment detection.

Signals extracted:
- Energy/RMS levels (baseline + spikes)
- Laughter detection (spectral analysis)
- Energy shifts (sudden increases/decreases)
- Speech density
- Silence patterns (meaningful vs dead space)
- Spectral features (brightness, spectral centroid)
"""

import asyncio
import math
import struct
import subprocess
import statistics
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import numpy as np

from .config import CONFIG, AudioConfig
from .job_manager import JobManager, JobState, JobErrorType


@dataclass
class AudioWindow:
    """Audio analysis for a time window."""
    start: float
    end: float
    rms_db: float
    energy_score: float
    spectral_centroid: float
    spectral_rolloff: float
    zero_crossing_rate: float
    is_speech: bool
    is_laughter: bool = False
    laughter_confidence: float = 0.0


@dataclass
class AudioSignal:
    """A detected audio signal."""
    signal_type: str  # "energy_spike", "laughter", "energy_shift", "silence", "speech_burst"
    timestamp: float
    strength: float  # 0-100
    details: Dict[str, Any]
    window: AudioWindow


class AudioAnalyzer:
    """
    Analyzes audio for viral moment signals.
    Uses FFmpeg to extract PCM audio, then analyzes in windows.
    """

    def __init__(self, config: AudioConfig = None):
        self.config = config or CONFIG.audio
        self.windows: List[AudioWindow] = []
        self.signals: List[AudioSignal] = []
        self._baseline_db: float = 0.0
        self._sample_rate = 16000
        self._window_samples = int(self._sample_rate * self.config.window_seconds)

    async def analyze(self, video_path: Path, job_id: str, job_manager: JobManager,
                     progress_callback: Optional[callable] = None) -> List[AudioSignal]:
        """
        Run full audio analysis on a video file.

        Args:
            video_path: Path to video file
            job_id: Job ID for tracking
            job_manager: Job manager for state updates
            progress_callback: Optional callback(progress, step)

        Returns:
            List of detected audio signals
        """
        job_manager.update_state(job_id, JobState.ANALYZING, 0, "Extracting audio...")

        # Extract audio as 16-bit PCM
        pcm_data = await self._extract_audio(video_path, job_id, job_manager)
        if not pcm_data:
            raise RuntimeError("Audio extraction failed")

        job_manager.update_state(job_id, JobState.ANALYZING, 20, "Analyzing audio windows...")

        # Analyze in windows
        self._analyze_windows(pcm_data)

        job_manager.update_state(job_id, JobState.ANALYZING, 60, "Detecting signals...")

        # Detect signals
        self._detect_energy_spikes()
        if self.config.laughter_detection_enabled:
            await self._detect_laughter(video_path, job_id, job_manager)
        self._detect_energy_shifts()
        self._detect_speech_patterns()

        job_manager.update_state(job_id, JobState.ANALYZING, 100, "Audio analysis complete")

        self.signals.sort(key=lambda s: s.timestamp)
        return self.signals

    async def _extract_audio(self, video_path: Path, job_id: str, job_manager: JobManager) -> bytes:
        """Extract audio as 16-bit PCM mono 16kHz."""
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", str(video_path),
            "-vn", "-ac", "1", "-ar", str(self._sample_rate),
            "-f", "s16le", "pipe:1"
        ]

        try:
            # Run in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(cmd, capture_output=True, timeout=600)
            )
            if result.returncode != 0:
                print(f"FFmpeg audio extraction error: {result.stderr.decode()}")
                return b""
            return result.stdout
        except subprocess.TimeoutExpired:
            raise RuntimeError("Audio extraction timed out")
        except Exception as e:
            raise RuntimeError(f"Audio extraction failed: {e}")

    def _analyze_windows(self, pcm_data: bytes):
        """Analyze PCM data in windows."""
        self.windows = []
        sample_width = 2
        bytes_per_window = self._window_samples * sample_width

        if len(pcm_data) < bytes_per_window:
            return

        rms_values = []

        for offset in range(0, len(pcm_data), bytes_per_window):
            chunk = pcm_data[offset:offset + bytes_per_window]
            if len(chunk) < 100:
                continue

            count = len(chunk) // sample_width
            try:
                samples = struct.unpack("<" + ("h" * count), chunk[:count * sample_width])
            except struct.error:
                continue

            if not samples:
                continue

            # RMS calculation
            square_sum = sum(s * s for s in samples)
            rms = math.sqrt(square_sum / len(samples))
            db = 20 * math.log10(max(rms, 1) / 32768)

            # Spectral features (simplified)
            spectral_centroid = self._spectral_centroid(samples)
            spectral_rolloff = self._spectral_rolloff(samples)
            zcr = self._zero_crossing_rate(samples)

            # Speech detection via VAD-like heuristic
            is_speech = db > self.config.silence_threshold_db and zcr > 0.01

            energy_score = max(0.0, min(self.config.max_energy_score, (db - self._baseline_db) * self.config.energy_multiplier))

            start = offset / (self._sample_rate * sample_width)
            end = min(start + self.config.window_seconds, len(pcm_data) / (self._sample_rate * sample_width))

            self.windows.append(AudioWindow(
                start=start,
                end=end,
                rms_db=db,
                energy_score=energy_score,
                spectral_centroid=spectral_centroid,
                spectral_rolloff=spectral_rolloff,
                zero_crossing_rate=zcr,
                is_speech=is_speech,
            ))

            rms_values.append(db)

        # Calculate baseline
        if rms_values:
            self._baseline_db = statistics.median(rms_values)
            # Recalculate energy scores with actual baseline
            for w in self.windows:
                w.energy_score = max(0.0, min(self.config.max_energy_score, (w.rms_db - self._baseline_db) * self.config.energy_multiplier))

    def _spectral_centroid(self, samples: List[int]) -> float:
        """Calculate spectral centroid (brightness)."""
        if len(samples) < 2:
            return 0.0
        # Simple FFT-based centroid
        try:
            fft = np.fft.rfft(np.array(samples, dtype=np.float32))
            magnitudes = np.abs(fft)
            freqs = np.fft.rfftfreq(len(samples), 1.0 / self._sample_rate)
            if magnitudes.sum() == 0:
                return 0.0
            return float(np.sum(freqs * magnitudes) / np.sum(magnitudes))
        except Exception:
            return 0.0

    def _spectral_rolloff(self, samples: List[int], percentile: float = 0.85) -> float:
        """Calculate spectral rolloff."""
        if len(samples) < 2:
            return 0.0
        try:
            fft = np.fft.rfft(np.array(samples, dtype=np.float32))
            magnitudes = np.abs(fft)
            cumsum = np.cumsum(magnitudes)
            total = cumsum[-1]
            if total == 0:
                return 0.0
            rolloff_idx = np.where(cumsum >= percentile * total)[0]
            if len(rolloff_idx) == 0:
                return float(self._sample_rate / 2)
            freqs = np.fft.rfftfreq(len(samples), 1.0 / self._sample_rate)
            return float(freqs[rolloff_idx[0]])
        except Exception:
            return 0.0

    def _zero_crossing_rate(self, samples: List[int]) -> float:
        """Calculate zero crossing rate."""
        if len(samples) < 2:
            return 0.0
        crossings = sum(1 for i in range(1, len(samples)) if samples[i] * samples[i-1] < 0)
        return crossings / len(samples)

    def _detect_energy_spikes(self):
        """Detect sudden energy increases."""
        if not self.windows:
            return

        for i, window in enumerate(self.windows):
            if window.energy_score > 15:  # Significant energy above baseline
                # Check if it's a spike (higher than neighbors)
                prev_energy = self.windows[i-1].energy_score if i > 0 else 0
                next_energy = self.windows[i+1].energy_score if i < len(self.windows)-1 else 0
                avg_neighbor = (prev_energy + next_energy) / 2

                if window.energy_score > avg_neighbor * 2 and window.energy_score > 20:
                    strength = min(100, window.energy_score * 2)
                    self.signals.append(AudioSignal(
                        signal_type="energy_spike",
                        timestamp=(window.start + window.end) / 2,
                        strength=strength,
                        details={
                            "rms_db": round(window.rms_db, 1),
                            "energy_score": round(window.energy_score, 1),
                            "baseline_db": round(self._baseline_db, 1),
                            "spectral_centroid": round(window.spectral_centroid, 0),
                        },
                        window=window,
                    ))

    async def _detect_laughter(self, video_path: Path, job_id: str, job_manager: JobManager):
        """
        Detect laughter using spectral analysis.
        Laughter typically has:
        - High zero crossing rate
        - Broad spectral energy
        - Periodic pattern
        - High spectral centroid
        """
        # For each window flagged as potential laughter, do deeper analysis
        candidate_windows = [w for w in self.windows if w.energy_score > 10 and w.zero_crossing_rate > 0.05]

        for window in candidate_windows:
            # Extract a slightly larger segment for analysis
            confidence = self._analyze_laughter_segment(window)
            if confidence > 0.6:
                window.is_laughter = True
                window.laughter_confidence = confidence
                strength = min(100, confidence * 100)
                self.signals.append(AudioSignal(
                    signal_type="laughter",
                    timestamp=(window.start + window.end) / 2,
                    strength=strength,
                    details={
                        "confidence": round(confidence, 2),
                        "zcr": round(window.zero_crossing_rate, 3),
                        "centroid": round(window.spectral_centroid, 0),
                    },
                    window=window,
                ))

    def _analyze_laughter_segment(self, window: AudioWindow) -> float:
        """Analyze a segment for laughter characteristics."""
        # Heuristic: laughter has high ZCR, high centroid, periodic energy
        zcr_score = min(1.0, window.zero_crossing_rate * 10)
        centroid_score = min(1.0, window.spectral_centroid / 4000)  # Normalize to ~4kHz
        energy_score = min(1.0, window.energy_score / 25)

        # Laughter typically: high ZCR + high centroid + moderate-high energy
        confidence = (zcr_score * 0.4 + centroid_score * 0.3 + energy_score * 0.3)
        return confidence

    def _detect_energy_shifts(self):
        """Detect sudden energy shifts (both up and down)."""
        if len(self.windows) < 3:
            return

        for i in range(1, len(self.windows) - 1):
            prev = self.windows[i-1].energy_score
            curr = self.windows[i].energy_score
            next_ = self.windows[i+1].energy_score

            # Sudden increase
            if curr > prev * 2.5 and curr > next_ * 1.5 and curr > 15:
                self.signals.append(AudioSignal(
                    signal_type="energy_shift_up",
                    timestamp=(self.windows[i].start + self.windows[i].end) / 2,
                    strength=min(100, curr * 1.5),
                    details={
                        "prev_energy": round(prev, 1),
                        "curr_energy": round(curr, 1),
                        "next_energy": round(next_, 1),
                        "shift_magnitude": round(curr - prev, 1),
                    },
                    window=self.windows[i],
                ))

            # Sudden drop (can indicate punchline/reaction)
            if prev > curr * 2.5 and prev > 15:
                self.signals.append(AudioSignal(
                    signal_type="energy_shift_down",
                    timestamp=(self.windows[i].start + self.windows[i].end) / 2,
                    strength=min(100, prev * 1.2),
                    details={
                        "prev_energy": round(prev, 1),
                        "curr_energy": round(curr, 1),
                        "drop_magnitude": round(prev - curr, 1),
                    },
                    window=self.windows[i],
                ))

    def _detect_speech_patterns(self):
        """Detect speech density patterns and meaningful pauses."""
        if not self.windows:
            return

        speech_windows = [w for w in self.windows if w.is_speech]

        if not speech_windows:
            return

        # Find dense speech regions
        for i, window in enumerate(self.windows):
            if not window.is_speech:
                continue

            # Look at surrounding 10 seconds
            radius = 10.0
            nearby = [w for w in self.windows
                     if abs(w.start - window.start) <= radius and w.is_speech]

            if len(nearby) >= 5:  # At least 5 speech windows in 20s
                density = len(nearby) / (2 * radius / self.config.window_seconds)
                if density > 0.7:  # High speech density
                    self.signals.append(AudioSignal(
                        signal_type="speech_burst",
                        timestamp=window.start,
                        strength=min(100, density * 50),
                        details={
                            "speech_windows": len(nearby),
                            "density": round(density, 2),
                        },
                        window=window,
                    ))

        # Detect meaningful pauses (silence surrounded by speech)
        for i in range(1, len(self.windows) - 1):
            if (self.windows[i-1].is_speech and
                not self.windows[i].is_speech and
                self.windows[i+1].is_speech):

                pause_duration = self.windows[i].end - self.windows[i].start
                if pause_duration >= self.config.min_speech_duration:
                    # This is a meaningful pause - could be comedic timing
                    self.signals.append(AudioSignal(
                        signal_type="meaningful_pause",
                        timestamp=(self.windows[i].start + self.windows[i].end) / 2,
                        strength=min(100, pause_duration * 15),
                        details={
                            "pause_duration": round(pause_duration, 1),
                            "surrounded_by_speech": True,
                        },
                        window=self.windows[i],
                    ))

    def get_signal_at_time(self, timestamp: float, radius: float = 10.0) -> List[AudioSignal]:
        """Get all signals near a timestamp."""
        return [
            s for s in self.signals
            if abs(s.timestamp - timestamp) <= radius
        ]

    def get_aggregate_score(self, timestamp: float, radius: float = 10.0) -> Dict[str, float]:
        """Get aggregate audio signal scores near a timestamp."""
        signals = self.get_signal_at_time(timestamp, radius)
        scores = {
            "energy_spike": 0.0,
            "laughter": 0.0,
            "energy_shift_up": 0.0,
            "energy_shift_down": 0.0,
            "speech_burst": 0.0,
            "meaningful_pause": 0.0,
        }

        for s in signals:
            if s.signal_type in scores:
                distance_factor = 1.0 - (abs(s.timestamp - timestamp) / radius)
                scores[s.signal_type] = max(scores[s.signal_type], s.strength * distance_factor)

        return scores

    def get_speech_segments(self, min_duration: float = 1.0) -> List[Tuple[float, float]]:
        """Get continuous speech segments."""
        segments = []
        current_start = None

        for window in self.windows:
            if window.is_speech:
                if current_start is None:
                    current_start = window.start
            else:
                if current_start is not None:
                    duration = window.start - current_start
                    if duration >= min_duration:
                        segments.append((current_start, window.start))
                    current_start = None

        # Handle speech at end
        if current_start is not None and self.windows:
            duration = self.windows[-1].end - current_start
            if duration >= min_duration:
                segments.append((current_start, self.windows[-1].end))

        return segments

    def get_silence_segments(self, max_duration: float = 30.0) -> List[Tuple[float, float]]:
        """Get silence segments (for dead space detection)."""
        segments = []
        current_start = None

        for window in self.windows:
            if not window.is_speech:
                if current_start is None:
                    current_start = window.start
            else:
                if current_start is not None:
                    duration = window.start - current_start
                    if duration <= max_duration:
                        segments.append((current_start, window.start))
                    current_start = None

        if current_start is not None and self.windows:
            duration = self.windows[-1].end - current_start
            if duration <= max_duration:
                segments.append((current_start, self.windows[-1].end))

        return segments