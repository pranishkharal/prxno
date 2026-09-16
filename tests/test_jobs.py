"""Tests for the durable job store and resource queue (Phases 5, 6, 9)."""

from __future__ import annotations

import asyncio
import threading
import unittest

from infra.concurrency import (
    CLASS_ANALYSIS,
    CLASS_DOWNLOAD,
    CLASS_ENCODE,
    ResourceGovernor,
)
from infra.config import INFRA_CONFIG
from infra.hardware import HardwareProfile
from infra.jobs import (
    JobCancelledError,
    JobKind,
    JobRecord,
    JobStatus,
    JobStore,
    ResourceQueue,
    is_transient,
)
from tests.helpers import TempDirMixin

WEAK = HardwareProfile(cpu_count=4, total_ram_gb=8.0)


class ClassificationTests(unittest.TestCase):

    def test_transient_errors_are_retryable(self):
        for message in ("connection reset by peer", "request timed out",
                        "HTTP 503 service temporarily unavailable",
                        "WinError 32 file in use"):
            self.assertTrue(is_transient(RuntimeError(message)), message)

    def test_permanent_errors_are_not_retryable(self):
        for message in ("invalid argument", "not a valid KICK URL", "unsupported codec"):
            self.assertFalse(is_transient(ValueError(message)), message)


class JobStoreTests(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.store = JobStore(root=self.dir("jobs"))

    def test_save_and_load_roundtrip(self):
        record = JobRecord(job_id="job_abc", kind=JobKind.CLIP_EDIT.value, user_id=42)
        self.store.save(record)
        loaded = self.store.load("job_abc")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.user_id, 42)
        self.assertEqual(loaded.status, JobStatus.QUEUED.value)

    def test_status_transitions_stamp_timestamps(self):
        record = JobRecord(job_id="job_t")
        record.mark(JobStatus.RUNNING)
        self.assertEqual(record.attempts, 1)
        self.assertIsNotNone(record.started_at)
        record.mark(JobStatus.COMPLETED)
        self.assertIsNotNone(record.finished_at)
        self.assertTrue(JobStatus.COMPLETED.terminal)

    def test_list_and_delete(self):
        self.store.save(JobRecord(job_id="one"))
        self.store.save(JobRecord(job_id="two"))
        self.assertEqual(len(self.store.all()), 2)
        self.assertTrue(self.store.delete("one"))
        self.assertEqual(len(self.store.all()), 1)

    def test_missing_file_returns_none(self):
        self.assertIsNone(self.store.load("nope"))

    def test_corrupt_job_file_is_skipped(self):
        (self.dir("jobs") / "broken.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(self.store.all(), [])

    def test_restart_recovery_requeues_running_jobs(self):
        record = JobRecord(job_id="job_crash", status=JobStatus.RUNNING.value, attempts=1)
        self.store.save(record)
        result = self.store.repair_orphans()
        self.assertIn("job_crash", result["requeued"])
        self.assertEqual(self.store.load("job_crash").status, JobStatus.QUEUED.value)

    def test_restart_recovery_fails_exhausted_jobs(self):
        record = JobRecord(job_id="job_dead", status=JobStatus.RUNNING.value,
                           attempts=3, max_attempts=3)
        self.store.save(record)
        result = self.store.repair_orphans()
        self.assertIn("job_dead", result["failed"])
        self.assertEqual(self.store.load("job_dead").status, JobStatus.FAILED.value)

    def test_restart_recovery_leaves_terminal_jobs_alone(self):
        record = JobRecord(job_id="job_done", status=JobStatus.COMPLETED.value)
        self.store.save(record)
        result = self.store.repair_orphans()
        self.assertEqual(result["requeued"], [])
        self.assertEqual(result["failed"], [])

    def test_sweep_terminal_removes_old_finished_jobs(self):
        record = JobRecord(job_id="job_old", status=JobStatus.COMPLETED.value)
        record.mark(JobStatus.COMPLETED)
        record.finished_at = 0.0
        self.store.save(record)
        self.store.save(JobRecord(job_id="job_live"))
        self.assertEqual(self.store.sweep_terminal(keep_seconds=60), 1)
        self.assertIsNone(self.store.load("job_old"))
        self.assertIsNotNone(self.store.load("job_live"))

class ResourceQueueTests(TempDirMixin, unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        self.backoff = INFRA_CONFIG.job_retry_backoff
        INFRA_CONFIG.job_retry_backoff = 0.01
        self.store = JobStore(root=self.dir("jobs"))
        self.governor = ResourceGovernor(profile=WEAK, overrides={"encodes": 2})
        self.queue = ResourceQueue(store=self.store, governor=self.governor)

    def tearDown(self):
        INFRA_CONFIG.job_retry_backoff = self.backoff
        super().tearDown()

    async def test_successful_job_is_recorded(self):
        record = self.queue.create(JobKind.CLIP_EDIT.value, user_id=7)
        result = await self.queue.run(record, CLASS_ENCODE, lambda: "done")
        self.assertEqual(result, "done")
        stored = self.store.load(record.job_id)
        self.assertEqual(stored.status, JobStatus.COMPLETED.value)
        self.assertEqual(stored.attempts, 1)
        self.assertEqual(stored.progress, 100)

    async def test_async_work_function_is_awaited(self):
        record = self.queue.create(JobKind.CLIP_EDIT.value)

        async def work():
            await asyncio.sleep(0.01)
            return "async-done"

        self.assertEqual(await self.queue.run(record, CLASS_ENCODE, work), "async-done")

    async def test_transient_failure_is_retried_then_succeeds(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise RuntimeError("connection reset by peer")
            return "recovered"

        record = self.queue.create(JobKind.CLIP_FETCH.value)
        result = await self.queue.run(record, CLASS_ENCODE, flaky)
        self.assertEqual(result, "recovered")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(self.store.load(record.job_id).status, JobStatus.COMPLETED.value)

    async def test_permanent_failure_is_not_retried(self):
        attempts = []

        def broken():
            attempts.append(1)
            raise ValueError("invalid argument")

        record = self.queue.create(JobKind.CLIP_FETCH.value)
        with self.assertRaises(ValueError):
            await self.queue.run(record, CLASS_ENCODE, broken)
        self.assertEqual(len(attempts), 1, "permanent errors must not loop")
        stored = self.store.load(record.job_id)
        self.assertEqual(stored.status, JobStatus.FAILED.value)
        self.assertIn("invalid argument", stored.error)

    async def test_cancelled_job_never_runs(self):
        record = self.queue.create(JobKind.CLIP_EDIT.value)
        self.assertTrue(self.queue.cancel(record.job_id))
        ran = []
        with self.assertRaises(JobCancelledError):
            await self.queue.run(record, CLASS_ENCODE, lambda: ran.append(1))
        self.assertEqual(ran, [])
        self.assertEqual(self.store.load(record.job_id).status, JobStatus.CANCELLED.value)

    async def test_resource_limit_is_enforced(self):
        state = {"current": 0, "max": 0}
        lock = asyncio.Lock()

        async def work():
            async with lock:
                state["current"] += 1
                state["max"] = max(state["max"], state["current"])
            await asyncio.sleep(0.15)
            async with lock:
                state["current"] -= 1
            return "ok"

        records = [self.queue.create(JobKind.CLIP_EDIT.value) for _ in range(6)]
        results = await asyncio.gather(
            *[self.queue.run(record, CLASS_ENCODE, work) for record in records]
        )
        self.assertEqual(results, ["ok"] * 6)
        self.assertLessEqual(state["max"], 2, "the class limit must bound concurrency")
        self.assertGreaterEqual(state["max"], 1)

    async def test_spawn_tracks_and_clears_the_task(self):
        record = self.queue.create(JobKind.CLIP_EDIT.value)

        async def work():
            await asyncio.sleep(0.05)
            return "spawned"

        task = self.queue.spawn(record, CLASS_ENCODE, work)
        self.assertIn(record.job_id, self.queue.active_job_ids())
        self.assertEqual(await task, "spawned")
        await asyncio.sleep(0.05)
        self.assertNotIn(record.job_id, self.queue.active_job_ids())

    async def test_cancel_stops_a_spawned_task(self):
        record = self.queue.create(JobKind.CLIP_EDIT.value)

        async def work():
            await asyncio.sleep(30)

        task = self.queue.spawn(record, CLASS_ENCODE, work)
        await asyncio.sleep(0.05)
        self.assertTrue(self.queue.cancel(record.job_id))
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.load(record.job_id).status, JobStatus.CANCELLED.value)

    async def test_downloads_have_a_dedicated_thread_pool(self):
        governor = ResourceGovernor(profile=WEAK)
        download_pool = governor.executor(CLASS_DOWNLOAD)
        self.assertIsNotNone(download_pool)
        self.assertIsNone(governor.executor(CLASS_ENCODE))
        self.assertIsNone(governor.executor(CLASS_ANALYSIS))

        started = await governor.run_in_class(
            CLASS_DOWNLOAD, lambda: threading.current_thread().name)
        self.assertTrue(started.startswith("dl"),
                        "downloads must run on the download pool")

        names = await asyncio.gather(*[
            governor.run_in_class(CLASS_DOWNLOAD, lambda: "pooled")
            for _ in range(3)
        ])
        self.assertEqual(names, ["pooled"] * 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)