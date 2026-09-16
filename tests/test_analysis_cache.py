"""Tests for the versioned analysis cache (Phase 3)."""

from __future__ import annotations

import json
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from infra.analysis_cache import (
    AnalysisCache,
    KIND_ANALYSIS,
    KIND_TRANSCRIPT,
    analysis_key,
    config_fingerprint,
)
from tests.helpers import TempDirMixin

SOURCE = "kick:clip:clip_aaa111"


class FingerprintTests(unittest.TestCase):

    def test_dict_fingerprint_is_order_independent(self):
        self.assertEqual(
            config_fingerprint({"a": 1, "b": 2}),
            config_fingerprint({"b": 2, "a": 1}),
        )

    def test_different_configs_differ(self):
        self.assertNotEqual(
            config_fingerprint({"window": 30}),
            config_fingerprint({"window": 10}),
        )

    def test_key_changes_with_model_and_version(self):
        base = analysis_key(SOURCE, KIND_TRANSCRIPT, "base", "1.0.0")
        self.assertNotEqual(base, analysis_key(SOURCE, KIND_TRANSCRIPT, "large", "1.0.0"))
        self.assertNotEqual(base, analysis_key(SOURCE, KIND_TRANSCRIPT, "base", "2.0.0"))
        self.assertNotEqual(base, analysis_key(SOURCE, KIND_ANALYSIS, "base", "1.0.0"))


class AnalysisCacheTests(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.cache = AnalysisCache(root=self.dir("analysis"))

    def test_miss_then_hit_computes_once(self):
        calls = []

        def compute():
            calls.append(1)
            return {"text": "hello world"}

        payload, hit = self.cache.get_or_compute(
            SOURCE, KIND_TRANSCRIPT, compute, model="base"
        )
        self.assertFalse(hit)
        self.assertEqual(payload, {"text": "hello world"})

        payload_again, hit_again = self.cache.get_or_compute(
            SOURCE, KIND_TRANSCRIPT, compute, model="base"
        )
        self.assertTrue(hit_again)
        self.assertEqual(payload_again, payload)
        self.assertEqual(len(calls), 1, "two jobs on one source must share one result")

    def test_concurrent_jobs_share_one_computation(self):
        calls = []
        lock = threading.Lock()

        def worker(_index):
            def compute():
                with lock:
                    calls.append(1)
                time.sleep(0.3)
                return {"text": "shared"}

            return self.cache.get_or_compute(SOURCE, KIND_TRANSCRIPT, compute, model="base")

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(8)))

        self.assertEqual(len(calls), 1, "8 simultaneous jobs must transcribe once")
        for payload, _hit in results:
            self.assertEqual(payload, {"text": "shared"})

    def test_different_model_invalidates(self):
        calls = []

        def compute():
            calls.append(1)
            return {"value": len(calls)}

        self.cache.get_or_compute(SOURCE, KIND_TRANSCRIPT, compute, model="base")
        _payload, hit = self.cache.get_or_compute(SOURCE, KIND_TRANSCRIPT, compute, model="large")
        self.assertFalse(hit, "an incompatible model must not silently reuse old results")
        self.assertEqual(len(calls), 2)

    def test_different_config_invalidates(self):
        calls = []

        def compute():
            calls.append(1)
            return {"value": len(calls)}

        self.cache.get_or_compute(SOURCE, KIND_ANALYSIS, compute, config={"window": 30})
        _payload, hit = self.cache.get_or_compute(
            SOURCE, KIND_ANALYSIS, compute, config={"window": 10}
        )
        self.assertFalse(hit)
        self.assertEqual(len(calls), 2)

    def test_kinds_are_independent(self):
        self.cache.put(SOURCE, KIND_TRANSCRIPT, {"text": "t"})
        self.cache.put(SOURCE, KIND_ANALYSIS, {"score": 5})
        self.assertEqual(self.cache.get(SOURCE, KIND_TRANSCRIPT), {"text": "t"})
        self.assertEqual(self.cache.get(SOURCE, KIND_ANALYSIS), {"score": 5})

    def test_other_sources_do_not_collide(self):
        self.cache.put(SOURCE, KIND_TRANSCRIPT, {"text": "one"})
        self.assertIsNone(self.cache.get("kick:clip:other", KIND_TRANSCRIPT))

    def test_corrupt_entry_is_ignored_and_recomputed(self):
        self.cache.put(SOURCE, KIND_TRANSCRIPT, {"text": "good"})
        for path in self.cache.source_dir(SOURCE).glob("*.json"):
            path.write_text("{not json", encoding="utf-8")

        calls = []

        def compute():
            calls.append(1)
            return {"text": "recovered"}

        payload, hit = self.cache.get_or_compute(SOURCE, KIND_TRANSCRIPT, compute)
        self.assertFalse(hit)
        self.assertEqual(payload, {"text": "recovered"})
        self.assertEqual(len(calls), 1)

    def test_envelope_records_processing_identity(self):
        path = self.cache.put(SOURCE, KIND_TRANSCRIPT, {"text": "x"}, model="base")
        envelope = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(envelope["source_id"], SOURCE)
        self.assertEqual(envelope["model"], "base")
        self.assertEqual(envelope["kind"], KIND_TRANSCRIPT)
        self.assertIn("config_fingerprint", envelope)

    def test_invalidate_source_and_stats(self):
        self.cache.put(SOURCE, KIND_TRANSCRIPT, {"text": "x"})
        self.assertEqual(self.cache.stats()["entries"], 1)
        self.assertTrue(self.cache.invalidate_source(SOURCE))
        self.assertEqual(self.cache.stats()["entries"], 0)

    def test_sweep_expired(self):
        self.cache.put(SOURCE, KIND_TRANSCRIPT, {"text": "x"})
        self.assertEqual(self.cache.sweep_expired(ttl_seconds=3600)["removed"], 0)
        self.assertEqual(self.cache.sweep_expired(ttl_seconds=-1)["removed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)