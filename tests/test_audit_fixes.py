"""
Regression and verification tests for Smart File Intelligence & Token Efficiency fixes.

Validates:
1. Provider usage metadata extraction and accumulation in llm.py
2. Internal metadata sanitization before sending to provider
3. Head + tail truncation preserving traceback and STDERR
4. Overlapping range read detection
5. Large identical read deduplication with safety preview
6. Cache invalidation with relative paths
7. Directory cache flushing on new task reset
8. Accurate UTF-8 byte tracking
"""
import builtins
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from arcdvncli import agent, tools, llm


class TestAuditFixes(unittest.TestCase):

    def setUp(self):
        self.orig_environ = dict(os.environ)
        self.temp_home = tempfile.mkdtemp()
        self.temp_proj = tempfile.mkdtemp()

        os.environ["HOME"] = self.temp_home
        os.environ["USERPROFILE"] = self.temp_home
        os.environ["ARCDVN_PROVIDER"] = "google"
        os.environ["ARCDVN_GOOGLE_API_KEY"] = "fake-key"
        os.environ["ARCDVN_MAX_STEPS"] = "15"

        self.orig_cwd = os.getcwd()
        os.chdir(self.temp_proj)
        tools.ROOT = Path(self.temp_proj).resolve()
        tools.invalidate_caches()
        tools.reset_task_metrics()
        llm.reset_llm_metrics()

        self.orig_input = builtins.input
        builtins.input = lambda *a: "y"

    def tearDown(self):
        os.chdir(self.orig_cwd)
        tools.ROOT = Path(self.orig_cwd).resolve()
        tools.invalidate_caches()
        tools.reset_task_metrics()
        llm.reset_llm_metrics()
        builtins.input = self.orig_input
        os.environ.clear()
        os.environ.update(self.orig_environ)
        shutil.rmtree(self.temp_home, ignore_errors=True)
        shutil.rmtree(self.temp_proj, ignore_errors=True)

    def test_llm_usage_captured_and_messages_sanitized(self):
        """Verify provider usage is parsed into task metrics and messages sanitized."""
        llm.reset_llm_metrics()
        captured_payloads = []

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "Halo!"}}],
            "usage": {
                "prompt_tokens": 42,
                "completion_tokens": 18,
                "total_tokens": 120,  # e.g., includes reasoning tokens
            }
        }

        def mock_post(url, *args, **kwargs):
            captured_payloads.append(kwargs.get("json", {}))
            return mock_resp

        with patch("requests.post", side_effect=mock_post):
            msg = llm.chat([
                {"role": "user", "content": "ping", "_internal_flag": 999}
            ])

        # 1. Returned message has _usage attached
        self.assertIn("_usage", msg)
        self.assertEqual(msg["_usage"]["prompt_tokens"], 42)

        # 2. Sent payload was sanitized: no keys starting with '_'
        sent_messages = captured_payloads[0].get("messages", [])
        self.assertNotIn("_internal_flag", sent_messages[0])

        # 3. LLM task metrics reflect provider usage
        metrics = llm.get_llm_task_metrics()
        self.assertTrue(metrics["has_usage"])
        self.assertEqual(metrics["prompt_tokens"], 42)
        self.assertEqual(metrics["completion_tokens"], 18)
        self.assertEqual(metrics["total_tokens"], 120)
        self.assertEqual(metrics["http_attempts"], 1)
        self.assertEqual(metrics["internal_retries"], 0)

    def test_truncation_preserves_head_and_tail_errors(self):
        """Verify _truncate_tool_result_for_llm preserves traceback header and final exception."""
        header = "Traceback (most recent call last):\n  File 'app.py', line 12, in run\n"
        middle = "  at function_call()\n" * 600  # large middle
        tail = "TypeError: cannot unpack non-iterable NoneType object"
        full_text = header + middle + tail

        self.assertGreater(len(full_text), 10000)
        truncated = agent._truncate_tool_result_for_llm(full_text, limit=4000)

        # Stays within configured limit (+ small notice buffer)
        self.assertLessEqual(len(truncated), 4000)
        # Preserves header
        self.assertIn("Traceback (most recent call last):", truncated)
        # Preserves critical final exception line
        self.assertIn("TypeError: cannot unpack non-iterable NoneType object", truncated)
        # Includes omission notice
        self.assertIn("dipotong", truncated)

    def test_overlapping_range_reads_detected(self):
        """Verify reading overlapping line ranges in the same task increments overlapping_read_count."""
        target = Path(self.temp_proj) / "lines.py"
        target.write_text("\n".join(f"line_{i} = {i}" for i in range(1, 101)), encoding="utf-8")

        tools.read_file("lines.py", start_line=1, end_line=50)
        tm1 = tools.get_task_metrics()
        self.assertEqual(tm1["read_count"], 1)
        self.assertEqual(tm1["overlapping_read_count"], 0)

        # Read overlapping range (25 to 75)
        tools.read_file("lines.py", start_line=25, end_line=75)
        tm2 = tools.get_task_metrics()
        self.assertEqual(tm2["read_count"], 2)
        self.assertEqual(tm2["repeated_read_count"], 0)
        self.assertEqual(tm2["overlapping_read_count"], 1, "Lines 25-50 overlap with lines 1-50")

    def test_large_identical_read_deduplicated_with_preview(self):
        """Verify reading the exact same large range twice in the same task returns preview reference."""
        target = Path(self.temp_proj) / "large.py"
        target.write_text("\n".join(f"val_{i} = 'some_data_string_{i}' * 5" for i in range(1, 120)), encoding="utf-8")

        # First read: returns full content
        r1 = tools.read_file("large.py")
        self.assertGreater(len(r1), 2000)

        # Second identical read: returns preview reference
        r2 = tools.read_file("large.py")
        self.assertIn("identik dengan pembacaan sebelumnya", r2)
        self.assertIn("Pratinjau awal", r2)
        self.assertLess(len(r2), len(r1))

        # Re-read with force=True returns full content
        r3 = tools.read_file("large.py", force=True)
        self.assertEqual(r3, r1)

    def test_cache_invalidation_with_relative_path(self):
        """Verify calling invalidate_caches with a relative path string clears resolved path entry."""
        target = Path(self.temp_proj) / "rel_cache.txt"
        target.write_text("v1", encoding="utf-8")

        res1 = tools.read_file("rel_cache.txt")
        self.assertEqual(res1, "v1")

        # Invalidate using relative string
        target.write_text("v2", encoding="utf-8")
        tools.invalidate_caches("rel_cache.txt")

        # Reads fresh content
        res2 = tools.read_file("rel_cache.txt")
        self.assertEqual(res2, "v2")

    def test_dir_cache_flushed_on_new_task(self):
        """Verify reset_task_metrics clears directory cache so new tasks see external changes."""
        tools.list_files(".")
        self.assertGreaterEqual(len(tools._DIR_CACHE), 1)

        # External file added
        (Path(self.temp_proj) / "new_external.txt").write_text("hello", encoding="utf-8")

        # New task starts
        tools.reset_task_metrics()
        self.assertEqual(len(tools._DIR_CACHE), 0, "Directory cache should be empty on task reset")

        listing = tools.list_files(".")
        self.assertIn("new_external.txt", listing)

    def test_utf8_byte_tracking_accuracy(self):
        """Verify record_tool_result_bytes accurately measures UTF-8 byte length."""
        tools.reset_task_metrics()
        # Emojis and unicode
        text = "Halo 🚀 Dunia 📖 (Indonesian: Bahasa)"
        tools.record_tool_result_bytes(text)
        tm = tools.get_task_metrics()
        expected_bytes = len(text.encode("utf-8"))
        self.assertEqual(tm["tool_result_bytes"], expected_bytes)
        self.assertGreater(expected_bytes, len(text), "UTF-8 bytes > char count for unicode/emojis")


if __name__ == "__main__":
    unittest.main()
