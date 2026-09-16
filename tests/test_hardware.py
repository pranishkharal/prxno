"""Tests for hardware detection, encoder selection and adaptive limits (Phases 7-8)."""

from __future__ import annotations

import unittest

from infra.concurrency import (
    CLASS_ANALYSIS,
    CLASS_DOWNLOAD,
    CLASS_ENCODE,
    ResourceGovernor,
    ResourceLimits,
    plan_limits,
)
from infra.hardware import (
    CPU_ENCODER,
    HardwareProfile,
    build_video_encode_args,
    encoder_family,
    substitute_hw_encoder,
)

WEAK = HardwareProfile(cpu_count=4, total_ram_gb=8.0, cuda_available=False, verified_encoders=[])
STRONG = HardwareProfile(cpu_count=16, total_ram_gb=32.0, cuda_available=False, verified_encoders=[])
GPU_BOX = HardwareProfile(cpu_count=16, total_ram_gb=64.0, cuda_available=True,
                          verified_encoders=["h264_nvenc"])


class LimitPlanningTests(unittest.TestCase):

    def test_limits_are_always_positive(self):
        for profile in (WEAK, STRONG, GPU_BOX):
            limits = plan_limits(profile)
            self.assertGreaterEqual(limits.downloads, 1)
            self.assertGreaterEqual(limits.analysis, 1)
            self.assertGreaterEqual(limits.encodes, 1)

    def test_weak_machine_is_conservative(self):
        limits = plan_limits(WEAK)
        self.assertLessEqual(limits.encodes, 2)
        self.assertEqual(limits.analysis, 1, "low RAM must not run Whisper in parallel")
        self.assertLessEqual(limits.encodes + limits.analysis, 3)

    def test_strong_machine_gets_more_throughput(self):
        weak = plan_limits(WEAK)
        strong = plan_limits(STRONG)
        self.assertGreater(strong.encodes, weak.encodes)
        self.assertGreater(strong.downloads, weak.downloads)

    def test_gpu_machine_uses_accelerated_budget(self):
        limits = plan_limits(GPU_BOX)
        self.assertEqual(GPU_BOX.preferred_encoder, "h264_nvenc")
        self.assertEqual(limits.reason, "h264_nvenc")
        self.assertGreater(limits.encodes, plan_limits(STRONG).encodes)

    def test_heavy_classes_never_exceed_usable_cores(self):
        for profile in (WEAK, STRONG, GPU_BOX, HardwareProfile(cpu_count=2, total_ram_gb=4.0)):
            limits = plan_limits(profile)
            usable = max(1, (profile.cpu_count or 2) - limits.reserved_cores)
            self.assertLessEqual(limits.encodes + limits.analysis, usable)

    def test_no_hardware_profile_assumes_no_gpu(self):
        bare = HardwareProfile(cpu_count=8, total_ram_gb=16.0)
        self.assertFalse(bare.has_hardware_encoder)
        self.assertEqual(bare.preferred_encoder, CPU_ENCODER)


class GovernorTests(unittest.TestCase):

    def test_governor_exposes_one_limit_per_class(self):
        governor = ResourceGovernor(profile=WEAK)
        for name in (CLASS_DOWNLOAD, CLASS_ANALYSIS, CLASS_ENCODE):
            self.assertGreaterEqual(governor.limit(name), 1)

    def test_explicit_overrides_win(self):
        governor = ResourceGovernor(profile=WEAK, overrides={"encodes": 3, "downloads": 4})
        self.assertEqual(governor.limit(CLASS_ENCODE), 3)
        self.assertEqual(governor.limit(CLASS_DOWNLOAD), 4)

    def test_semaphores_are_stable_per_class(self):
        governor = ResourceGovernor(profile=STRONG)
        self.assertIs(governor.semaphore(CLASS_ENCODE), governor.semaphore(CLASS_ENCODE))
        self.assertIsNot(governor.semaphore(CLASS_ENCODE), governor.semaphore(CLASS_ANALYSIS))

    def test_unknown_class_is_rejected(self):
        with self.assertRaises(KeyError):
            ResourceLimits().for_class("nonsense")


class EncoderSelectionTests(unittest.TestCase):

    def test_substitution_replaces_cpu_encoder(self):
        command = [
            "ffmpeg", "-y", "-i", "in.mp4", "-c:v", "libx264", "-preset", "medium",
            "-crf", "20", "-c:a", "aac", "out.mp4",
        ]
        rewritten, changed = substitute_hw_encoder(command, "h264_nvenc")
        self.assertTrue(changed)
        self.assertIn("h264_nvenc", rewritten)
        self.assertNotIn("libx264", rewritten)
        self.assertNotIn("medium", rewritten)
        self.assertIn("-cq", rewritten)
        self.assertIn("20", rewritten)
        self.assertIn("p4", rewritten, "medium must map to nvenc preset p4")
        self.assertEqual(rewritten[-3:], ["-c:a", "aac", "out.mp4"])
        self.assertEqual(rewritten.count("-c:v"), 1)

    def test_substitution_preserves_quality_flags_first(self):
        command = ["ffmpeg", "-i", "in.mp4", "-c:v", "libx264", "-crf", "23",
                   "-preset", "veryslow", "out.mp4"]
        rewritten, changed = substitute_hw_encoder(command, "h264_qsv")
        self.assertTrue(changed)
        self.assertIn("23", rewritten)
        self.assertIn("-global_quality", rewritten)

    def test_substitution_is_a_no_op_without_libx264(self):
        command = ["ffmpeg", "-i", "in.mp4", "-c", "copy", "out.mp4"]
        rewritten, changed = substitute_hw_encoder(command, "h264_nvenc")
        self.assertFalse(changed)
        self.assertEqual(rewritten, command)

    def test_substitution_is_a_no_op_for_cpu_encoder(self):
        command = ["ffmpeg", "-c:v", "libx264", "-crf", "20", "out.mp4"]
        rewritten, changed = substitute_hw_encoder(command, CPU_ENCODER)
        self.assertFalse(changed)
        self.assertEqual(rewritten, command)

    def test_encode_args_per_family(self):
        self.assertIn("-crf", build_video_encode_args(CPU_ENCODER, "balanced", 20))
        self.assertIn("-cq", build_video_encode_args("h264_nvenc", "balanced", 20))
        self.assertIn("-global_quality", build_video_encode_args("h264_qsv", "fast", 20))
        self.assertEqual(encoder_family("h264_nvenc"), "nvidia")
        self.assertEqual(encoder_family("h264_qsv"), "intel")
        self.assertEqual(encoder_family("h264_amf"), "amd")
        self.assertEqual(encoder_family(CPU_ENCODER), "cpu")

    def test_detection_does_not_require_a_gpu(self):
        profile = HardwareProfile.detect(probe_encoders=False, use_cache=False)
        self.assertGreaterEqual(profile.cpu_count, 1)
        self.assertIn(profile.preferred_encoder, [CPU_ENCODER, "h264_nvenc", "h264_qsv",
                                                  "h264_amf", "h264_vaapi"])


if __name__ == "__main__":
    unittest.main(verbosity=2)