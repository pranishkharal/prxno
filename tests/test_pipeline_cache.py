"""Tests for transcript serialisation and per-source analysis reuse."""

from __future__ import annotations

import json
import threading
import unittest

from infra.analysis_cache import AnalysisCache, KIND_TRANSCRIPT
from tests.helpers import TempDirMixin
from vod_intelligence.transcript_analyzer import (
    TranscriptAnalysis,
    TranscriptAnalyzer,
    TranscriptSegment,
    TranscriptWord,
    transcript_from_dict,
    transcript_to_dict,
)

SOURCE = "kick:vod:123456"


def build_analysis(text="hello world"):
    return TranscriptAnalysis(
        full_text=text,
        segments=[
            TranscriptSegment(
                text=text,
                start=0.0,
                end=1.5,
                words=[
                    TranscriptWord(word="hello", start=0.0, end=0.7),
                    TranscriptWord(word="world", start=0.7, end=1.5),
                ],
            )
        ],
        duration=1.5,
        word_count=2,
        language="en",
        topics=[{"topic": "test"}],
        emotions=[{"emotion": "neutral"}],
        storytelling_score=0.3,
        hook_candidates=[],
        payoff_candidates=[],
        controversy_score=0.0,
        surprise_score=0.0,
        named_entities=[],
        questions=[],
        arguments=[],
        key_phrases=["hello world"],
    )


class TranscriptSerialisationTests(unittest.TestCase):

    def test_dict_round_trip_preserves_the_transcript(self):
        original = build_analysis()
        restored = transcript_from_dict(transcript_to_dict(original))
        self.assertEqual(restored.full_text, original.full_text)
        self.assertEqual(restored.word_count, original.word_count)
        self.assertEqual(restored.language, "en")
        self.assertEqual(restored.key_phrases, ["hello world"])
        self.assertEqual(restored.topics, [{"topic": "test"}])
        self.assertEqual(len(restored.segments), 1)
        self.assertEqual([w.word for w in restored.segments[0].words], ["hello", "world"])
        self.assertAlmostEqual(restored.segments[0].words[1].end, 1.5)

    def test_dict_is_json_serialisable(self):
        json.dumps(transcript_to_dict(build_analysis()))

    def test_from_dict_tolerates_missing_fields(self):
        restored = transcript_from_dict({})
        self.assertEqual(restored.full_text, "")
        self.assertEqual(restored.segments, [])
        self.assertEqual(restored.language, "en")

    def test_malformed_segments_do_not_crash(self):
        restored = transcript_from_dict({"segments": [{"text": "no words here"}]})
        self.assertEqual(len(restored.segments), 1)
        self.assertEqual(restored.segments[0].words, [])


class TranscriptFileRoundTripTests(TempDirMixin, unittest.TestCase):

    def test_save_then_load_analysis_file(self):
        analyzer = TranscriptAnalyzer()
        target = self.dir("out") / "transcript.json"
        analyzer.save_analysis(build_analysis("saved text"), target)
        self.assertTrue(target.exists())
        loaded = analyzer.load_analysis(target)
        self.assertEqual(loaded.full_text, "saved text")
        self.assertEqual(len(loaded.segments), 1)
        self.assertEqual(loaded.storytelling_score, 0.3)