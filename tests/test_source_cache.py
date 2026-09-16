"""Tests for the deduplicating source cache (Phase 2)."""

from __future__ import annotations

import shutil
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from infra.config import INFRA_CONFIG
from infra.locking import LockBusyError, SourceValidationError, source_lock
from infra.source_cache import SourceCache, canonical_source_id
from infra.workspace import JobWorkspace
from tests.helpers import TempDirMixin, make_video

CLIP_URL_A = "https://kick.com/n3on/clips/clip_AAA111"
CLIP_URL_B = "https://kick.com/adinross/clips/clip_BBB222"


class CanonicalSourceIdTests(unittest.TestCase):

    def test_clip_url(self):
        self.assertEqual(canonical_source_id(CLIP_URL_A), "kick:clip:clip_aaa111")

    def test_vod_url(self):
        url = "https://kick.com/xqc/videos/123456"
        self.assertEqual(canonical_source_id(url), "kick:vod:123456")

    def test_identity_is_independent_of_query_string(self):
        self.assertEqual(
            canonical_source_id(CLIP_URL_A + "?ref=discord"),
            canonical_source_id(CLIP_URL_A),
        )

    def test_non_kick_url_is_stable(self):
        first = canonical_source_id("https://example.com/video/7")
        second = canonical_source_id("https://example.com/video/7")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("url:"))

    def test_empty_url_is_rejected(self):
        with self.assertRaises(ValueError):
            canonical_source_id("   ")


class SourceCacheTests(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.cache = SourceCache(root=self.dir("cache", "sources"), ttl_seconds=86400)
        self.fixture = make_video(self.dir("fixture") / "clip.mp4")

    def _producer(self, tally=None, sleep=0.0, name="source.mp4"):
        fixture = self.fixture

        def producer(destination):
            if tally is not None:
                tally.append(1)
            if sleep:
                time.sleep(sleep)
            target = destination.parent / name
            shutil.copy2(str(fixture), str(target))
            return target

        return producer

    def test_cache_miss_then_hit_downloads_once(self):
        tally = []
        first = self.cache.fetch(CLIP_URL_A, self._producer(tally))
        second = self.cache.fetch(CLIP_URL_A, self._producer(tally))

        self.assertEqual(len(tally), 1, "a cache hit must not download again")
        self.assertEqual(first, second)
        self.assertTrue(first.exists())
        self.assertGreater(first.stat().st_size, INFRA_CONFIG.source_min_bytes)

    def test_producer_template_filename_is_normalised(self):
        produced = self.cache.fetch(
            CLIP_URL_A, self._producer(name="kick_clip_ZZZ999.mp4")
        )
        self.assertEqual(produced.name, "source.mp4")

    def test_concurrent_same_source_downloads_exactly_once(self):
        tally = []
        lock = threading.Lock()

        def worker(_index):
            def producer(destination):
                with lock:
                    tally.append(1)
                time.sleep(0.4)
                target = destination.parent / "source.mp4"
                shutil.copy2(str(self.fixture), str(target))
                return target

            return self.cache.fetch(CLIP_URL_A, producer)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(worker, range(8)))

        self.assertEqual(len(tally), 1, "8 simultaneous requests must cause 1 download")
        self.assertEqual(len(set(results)), 1)

    def test_two_distinct_sources_download_twice(self):
        tally = []
        self.cache.fetch(CLIP_URL_A, self._producer(tally))
        self.cache.fetch(CLIP_URL_B, self._producer(tally))
        self.assertEqual(len(tally), 2)

    def test_truncated_download_is_rejected(self):
        def tiny(destination):
            destination.write_bytes(b"not really a video")
            return destination

        with self.assertRaises(SourceValidationError):
            self.cache.fetch(CLIP_URL_A, tiny, source_id="kick:clip:tiny")
        self.assertIsNone(self.cache.lookup("kick:clip:tiny", log_hit=False))

    def test_undecodable_payload_is_rejected(self):
        def garbage(destination):
            destination.write_bytes(b"\x00" * 40000)
            return destination

        with self.assertRaises(SourceValidationError):
            self.cache.fetch(CLIP_URL_A, garbage, source_id="kick:clip:garbage")
        self.assertIsNone(self.cache.lookup("kick:clip:garbage", log_hit=False))

    def test_corrupt_entry_is_evicted_and_refetched(self):
        tally = []
        source_id = canonical_source_id(CLIP_URL_A)
        self.cache.fetch(CLIP_URL_A, self._producer(tally))

        media = self.cache.find_media(source_id)
        media.write_bytes(b"corrupted")

        self.assertIsNone(self.cache.lookup(source_id, log_hit=False))
        recovered = self.cache.fetch(CLIP_URL_A, self._producer(tally))

        self.assertEqual(len(tally), 2)
        self.assertGreater(recovered.stat().st_size, INFRA_CONFIG.source_min_bytes)

    def test_link_into_gives_each_job_its_own_name(self):
        source_id = canonical_source_id(CLIP_URL_A)
        cached = self.cache.fetch(CLIP_URL_A, self._producer())

        job_one = self.cache.link_into(source_id, self.dir("job1") / "clip.mp4")
        job_two = self.cache.link_into(source_id, self.dir("job2") / "clip.mp4")

        self.assertNotEqual(job_one, job_two)
        self.assertTrue(job_one.exists() and job_two.exists())

        job_one.unlink()
        self.assertTrue(cached.exists(), "deleting a job copy must not delete the cache")
        self.assertTrue(job_two.exists())

    def test_workspace_cleanup_never_deletes_shared_cache(self):
        source_id = canonical_source_id(CLIP_URL_A)
        cached = self.cache.fetch(CLIP_URL_A, self._producer())

        workspace = JobWorkspace.create("jobA", root=self.dir("jobs"))
        self.cache.link_into(source_id, workspace.input_path("clip.mp4"))
        workspace.cleanup()

        self.assertFalse(workspace.exists())
        self.assertTrue(cached.exists())
        self.assertIsNotNone(self.cache.lookup(source_id, log_hit=False))

    def test_busy_lock_raises_instead_of_blocking_forever(self):
        source_id = canonical_source_id(CLIP_URL_A)
        holder = source_lock(self.cache.lock_path(source_id), timeout=5)
        holder.acquire()
        original = INFRA_CONFIG.cache_lock_timeout
        INFRA_CONFIG.cache_lock_timeout = 0.4
        try:
            with self.assertRaises(LockBusyError):
                self.cache.fetch(CLIP_URL_A, self._producer())
        finally:
            INFRA_CONFIG.cache_lock_timeout = original
            holder.release()

    def test_sweep_expired_removes_only_old_entries(self):
        self.cache.fetch(CLIP_URL_A, self._producer())
        result = self.cache.sweep_expired(ttl_seconds=3600)
        self.assertEqual(result["removed"], 0)

        result = self.cache.sweep_expired(ttl_seconds=-1)
        self.assertEqual(result["removed"], 1)
        self.assertGreater(result["bytes_freed"], 0)

    def test_stats_reports_entries(self):
        self.cache.fetch(CLIP_URL_A, self._producer())
        stats = self.cache.stats()
        self.assertEqual(stats["entries"], 1)
        self.assertGreater(stats["bytes"], 0)

    def test_get_for_job_dedupes_and_isolates_end_to_end(self):
        """The exact API download_kick_clip() now uses, under concurrency."""
        tally = []
        lock = threading.Lock()

        def worker(index):
            def producer(destination):
                with lock:
                    tally.append(1)
                time.sleep(0.3)
                target = destination.parent / "source.mp4"
                shutil.copy2(str(self.fixture), str(target))
                return target

            return self.cache.get_for_job(
                CLIP_URL_A, producer, self.dir("job%d" % index) / "clip.mp4"
            )

        with ThreadPoolExecutor(max_workers=5) as pool:
            outcomes = list(pool.map(worker, range(5)))

        self.assertEqual(len(tally), 1, "5 jobs on one clip must download exactly once")

        paths = [path for path, _source_id in outcomes]
        source_ids = {source_id for _path, source_id in outcomes}
        self.assertEqual(len(set(paths)), 5, "each job must get its own file")
        self.assertEqual(len(source_ids), 1, "all jobs must share one source identity")
        for path in paths:
            self.assertTrue(path.exists())

        # Deleting every job copy must leave the shared cache intact.
        for path in paths:
            path.unlink()
        self.assertIsNotNone(
            self.cache.lookup(canonical_source_id(CLIP_URL_A), log_hit=False)
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)