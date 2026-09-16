"""Tests for the FFmpeg execution layer (Phase 7)."""

from __future__ import annotations

import threading
import time
import unittest

from infra.config import INFRA_CONFIG
from infra.ffmpeg_runner import (
    FFmpegCancelled,
    FFmpegError,
    FFmpegOutputError,
    FFmpegRunner,
    FFmpegTimeout,
)
from infra.hardware import CPU_ENCODER, HardwareProfile
from tests.helpers import TempDirMixin


class FFmpegRunnerTests(TempDirMixin, unittest.TestCase):

    def setUp(self):
        super().setUp()
        self.runner = FFmpegRunner(profile=HardwareProfile(cpu_count=8))
        self._original_hw = INFRA_CONFIG.auto_hw_encode
        INFRA_CONFIG.auto_hw_encode = "false"

    def tearDown(self):
        INFRA_CONFIG.auto_hw_encode = self._original_hw
        super().tearDown()

    def _encode(self, output, seconds=1, size="320x240"):
        return [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc=size=%s:rate=15:duration=%s" % (size, seconds),
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(output),
        ]

    def _long_encode(self, output):
        return [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=600",
            "-c:v", "libx264", "-preset", "slow", "-pix_fmt", "yuv420p",
            str(output),
        ]

    def test_successful_run_creates_validated_output(self):
        output = self.dir("out") / "ok.mp4"
        result = self.runner.run(
            self._encode(output), timeout=120, outputs=[output],
            check_duration=True, echo=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(output.exists())
        self.assertGreater(output.stat().st_size, 0)
        self.assertEqual(result.encoder, CPU_ENCODER)
        self.assertFalse(result.hardware_accelerated)
        self.assertEqual(self.runner.running(), 0, "the registry must be drained")

    def test_bad_input_raises_typed_error_with_stderr(self):
        bogus = self.dir("out") / "missing.mp4"
        with self.assertRaises(FFmpegError) as caught:
            self.runner.run(
                ["ffmpeg", "-y", "-i", str(bogus), "-c:v", "libx264",
                 str(self.dir("out") / "never.mp4")],
                timeout=60, echo=False,
            )
        self.assertNotIsInstance(caught.exception, FFmpegTimeout)
        self.assertTrue(caught.exception.stderr_tail, "stderr must be captured")
        self.assertEqual(self.runner.running(), 0)
    def test_success_is_not_inferred_from_return_code_alone(self):
        """A zero exit code with no usable output must still be a failure."""
        missing = self.dir("out") / "never-written.mp4"
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-f", "lavfi", "-i", "testsrc=size=128x128:rate=10:duration=1",
            "-c:v", "libx264", "-preset", "ultrafast", "-f", "null", "-",
        ]
        with self.assertRaises(FFmpegOutputError):
            self.runner.run(command, timeout=60, outputs=[missing], echo=False)

    def test_timeout_terminates_the_process(self):
        output = self.dir("out") / "slow.mp4"
        started = time.time()
        with self.assertRaises(FFmpegTimeout):
            self.runner.run(self._long_encode(output), timeout=2, echo=False)
        self.assertLess(time.time() - started, 60, "the encoder must be killed, not awaited")
        self.assertEqual(self.runner.running(), 0)

    def test_cancellation_stops_a_running_encode(self):
        output = self.dir("out") / "cancelled.mp4"
        captured = {}

        def worker():
            try:
                self.runner.run(
                    self._long_encode(output), timeout=300,
                    cancel_token="token-1", echo=False,
                )
                captured["result"] = "completed"
            except BaseException as error:
                captured["error"] = error

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        time.sleep(1.0)
        self.assertTrue(self.runner.cancel("token-1"), "a live process must be cancellable")
        thread.join(timeout=45)

        self.assertIn("error", captured)
        self.assertIsInstance(captured["error"], FFmpegCancelled)
        self.assertEqual(self.runner.running(), 0)

    def test_cancel_all_drains_the_registry(self):
        captured = []

        def worker(name):
            try:
                self.runner.run(
                    self._long_encode(self.dir("out") / (name + ".mp4")),
                    timeout=300, cancel_token=name, echo=False,
                )
            except BaseException as error:
                captured.append(error)

        threads = [threading.Thread(target=worker, args=(n,), daemon=True) for n in ("a", "b")]
        for thread in threads:
            thread.start()
        time.sleep(1.0)
        self.assertEqual(self.runner.cancel_all(), 2)
        for thread in threads:
            thread.join(timeout=45)
        self.assertEqual(len(captured), 2)
        self.assertTrue(all(isinstance(error, FFmpegCancelled) for error in captured))
        self.assertEqual(self.runner.running(), 0)

    def test_cancel_unknown_token_is_harmless(self):
        self.assertFalse(self.runner.cancel("does-not-exist"))

class HardwareSubstitutionTests(unittest.TestCase):

    def setUp(self):
        self._original_hw = INFRA_CONFIG.auto_hw_encode

    def tearDown(self):
        INFRA_CONFIG.auto_hw_encode = self._original_hw

    def test_disabled_by_default_keeps_the_cpu_path(self):
        INFRA_CONFIG.auto_hw_encode = "false"
        gpu_box = HardwareProfile(verified_encoders=["h264_nvenc"])
        runner = FFmpegRunner(profile=gpu_box)
        command = ["ffmpeg", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "o.mp4"]
        prepared, encoder, hardware = runner.prepare(command)
        self.assertFalse(hardware)
        self.assertEqual(encoder, CPU_ENCODER)
        self.assertEqual(prepared, command)

    def test_auto_mode_uses_a_verified_encoder(self):
        INFRA_CONFIG.auto_hw_encode = "auto"
        gpu_box = HardwareProfile(verified_encoders=["h264_nvenc"])
        runner = FFmpegRunner(profile=gpu_box)
        command = ["ffmpeg", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "o.mp4"]
        prepared, encoder, hardware = runner.prepare(command)
        self.assertTrue(hardware)
        self.assertEqual(encoder, "h264_nvenc")
        self.assertIn("h264_nvenc", prepared)
        self.assertNotIn("libx264", prepared)

    def test_auto_mode_falls_back_when_no_encoder_is_verified(self):
        INFRA_CONFIG.auto_hw_encode = "auto"
        cpu_box = HardwareProfile(cpu_count=8, verified_encoders=[])
        runner = FFmpegRunner(profile=cpu_box)
        command = ["ffmpeg", "-c:v", "libx264", "-crf", "20", "o.mp4"]
        prepared, encoder, hardware = runner.prepare(command)
        self.assertFalse(hardware)
        self.assertEqual(encoder, CPU_ENCODER)
        self.assertEqual(prepared[:6], command)
        self.assertIn("-threads", prepared)

    def test_cpu_thread_cap_is_bounded_and_respects_explicit_threads(self):
        INFRA_CONFIG.auto_hw_encode = "false"
        runner = FFmpegRunner(profile=HardwareProfile(cpu_count=16))
        command = ["ffmpeg", "-i", "in.mp4", "-c:v", "libx264", "o.mp4"]
        prepared, _, hardware = runner.prepare(command)
        self.assertFalse(hardware)
        threads = prepared[prepared.index("-threads") + 1]
        self.assertTrue(1 <= int(threads) <= 8)

        explicit = ["ffmpeg", "-i", "in.mp4", "-c:v", "libx264",
                    "-threads", "2", "o.mp4"]
        prepared, _, _ = runner.prepare(explicit)
        self.assertEqual(prepared, explicit)

    def test_cpu_thread_cap_is_skipped_for_hardware_encodes(self):
        INFRA_CONFIG.auto_hw_encode = "auto"
        gpu_box = HardwareProfile(cpu_count=16,
                                  verified_encoders=["h264_nvenc"])
        runner = FFmpegRunner(profile=gpu_box)
        prepared, encoder, hardware = runner.prepare(
            ["ffmpeg", "-i", "in.mp4", "-c:v", "libx264", "o.mp4"])
        self.assertTrue(hardware)
        self.assertEqual(encoder, "h264_nvenc")
        self.assertNotIn("-threads", prepared)

    def test_auto_mode_without_a_verified_encoder_is_disabled(self):
        INFRA_CONFIG.auto_hw_encode = "auto"
        cpu_box = HardwareProfile(verified_encoders=[])
        self.assertFalse(FFmpegRunner(profile=cpu_box).hardware_enabled())

    def test_explicit_true_still_requires_verification(self):
        INFRA_CONFIG.auto_hw_encode = "true"
        cpu_box = HardwareProfile(verified_encoders=[])
        self.assertFalse(FFmpegRunner(profile=cpu_box).hardware_enabled())
        gpu_box = HardwareProfile(verified_encoders=["h264_qsv"])
        self.assertTrue(FFmpegRunner(profile=gpu_box).hardware_enabled())


if __name__ == "__main__":
    unittest.main(verbosity=2)