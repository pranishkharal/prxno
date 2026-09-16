"""Static integration guards for main.py.

main.py cannot be imported inside a test process: importing it starts
background threads and calls ``client.run()`` at module level. These tests
inspect the live source with the ``ast`` module instead, which is enough to
prove the new infrastructure is genuinely wired in, and that behaviour we must
not break is still present.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

MAIN_PATH = Path(__file__).resolve().parent.parent / "main.py"
BOM = chr(0xFEFF)


def load_source_and_tree():
    source = MAIN_PATH.read_text(encoding="utf-8")
    if source.startswith(BOM):
        source = source[1:]
    return source, ast.parse(source)


def find_function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def function_source(source, node):
    lines = source.splitlines()
    return "\n".join(lines[node.lineno - 1: node.end_lineno])


class MainSourceGuards(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.source, cls.tree = load_source_and_tree()

    def body_of(self, name):
        node = find_function(self.tree, name)
        self.assertIsNotNone(node, "main.py must still define %s()" % name)
        return function_source(self.source, node)

    def test_main_is_syntactically_valid(self):
        self.assertGreater(len(self.source), 1000)

    def test_run_ffmpeg_delegates_to_the_safe_runner(self):
        body = self.body_of("run_ffmpeg")
        self.assertIn("get_ffmpeg_runner()", body)
        self.assertIn(".run(", body)
        self.assertNotIn("subprocess.Popen", body, "the blocking Popen loop must be gone")

    def test_download_is_deduplicated_through_the_source_cache(self):
        body = self.body_of("download_kick_clip")
        self.assertIn("get_source_cache()", body)
        self.assertIn("get_for_job(", body)

    def test_partial_downloads_never_use_the_final_filename(self):
        body = self.body_of("download_kick_clip")
        self.assertIn("\"nopart\": False", body)

    def test_per_job_upload_folder_is_unique(self):
        body = self.body_of("download_kick_clip")
        self.assertIn("uuid.uuid4().hex", body)

    def test_cancellation_api_exists_in_the_runner(self):
        runner = (MAIN_PATH.parent / "infra" / "ffmpeg_runner.py").read_text(encoding="utf-8")
        self.assertIn("def cancel(self, token", runner)
        self.assertIn("def cancel_all(self", runner)
        self.assertIn("_terminate(", runner)

    def test_encoder_concurrency_is_adaptive(self):
        body = self.body_of("run_encode_job")
        self.assertIn("get_governor()", body)
        self.assertIn("CLASS_ENCODE", body)
        self.assertNotIn("Semaphore(", body)

    def test_download_concurrency_is_adaptive(self):
        body = self.body_of("run_download_job")
        self.assertIn("CLASS_DOWNLOAD", body)

    def test_transcription_has_its_own_resource_class(self):
        self.assertIn("CLASS_ANALYSIS", self.body_of("run_analysis_job"))
        self.assertIn("run_analysis_job", self.body_of("generate_caption_file"))

    def test_hardcoded_semaphores_are_gone(self):
        self.assertNotIn("EDIT_SEMAPHORE", self.source)
        self.assertNotIn("DOWNLOAD_SEMAPHORE", self.source)

    def test_no_guessed_cleanup_paths_remain(self):
        self.assertNotIn(chr(34) + "kick_{user_id}" + chr(34) + " / " + chr(34) + "kick_clip.mp4" + chr(34), self.source)
        self.assertNotIn("kick_{message.author.id}", self.source)
        self.assertIn("cleanup_stale_kick_uploads", self.source)

    def test_916_master_canvas_is_preserved(self):
        self.assertIn(chr(34) + "9:16" + chr(34) + ": (1080, 1920)", self.source)
        self.assertIn("pad=1080:1920:(1080-iw)/2:(1920-ih)/2:color=black", self.source)
        self.assertIn("9:16", self.body_of("edit_video"))

    def test_original_commands_and_features_still_exist(self):
        for name in (
            "on_message", "edit_video", "remove_silence", "cleanup_job_files",
            "handle_clip_command", "handle_download_command", "handle_upload_command",
            "handle_vod_command", "handle_live_command", "start_kick_edit",
            "generate_caption_file", "download_kick_clip", "find_overlay_for_streamer",
        ):
            self.assertIsNotNone(find_function(self.tree, name), "missing function: %s" % name)

    def test_startup_primes_hardware_and_runs_cache_maintenance(self):
        self.assertIn("_prime_infrastructure", self.source)
        body = self.body_of("_prime_infrastructure")
        self.assertIn("HardwareProfile.detect", body)
        self.assertIn("sweep_expired", body)
        self.assertIn("sweep_stale_workspaces", body)

    def test_no_discord_token_is_hardcoded(self):
        pattern = r"[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}"
        self.assertEqual(re.findall(pattern, self.source), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)