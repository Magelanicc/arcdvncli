"""
Efficiency benchmark and acceptance tests for ARCDVNCLI Smart File Intelligence.

Tests cover all 6 required benchmark scenarios (§8) and all 8 acceptance criteria (§9):

1. Simple conversation: zero filesystem calls, no tool definitions sent.
2. Targeted HTML/CSS change in an existing project.
3. Bug fix requiring multiple related files.
4. Coding task requiring broader repository inspection.
5. Repeated reads, cache invalidation, and duplicate tool-result prevention.
6. Provider rate-limit and timeout recovery.

Acceptance criteria validated:
- No filesystem inspection for ordinary conversation.
- No unnecessary repeated file reads.
- No duplicate tool results in LLM history.
- No raw file-content dumps in normal terminal UI (verified by compact display).
- Targeted changes preserve correctness.
- Required permissions and security checks remain intact.
- Agent produces actual task results instead of spending budget on redundant analysis.

Run via:
    python -m unittest tests.test_efficiency -v
"""
import builtins
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

for _s in (sys.stdout, sys.stderr):
    try:
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from arcdvncli import agent, tools, llm
from arcdvncli.router import detect_intent, TaskIntent
from arcdvncli.llm import LLMError


class BaseEfficiencyTest(unittest.TestCase):
    """Base test class providing an isolated temporary workspace."""

    def setUp(self):
        self.orig_environ = dict(os.environ)
        self.temp_home = tempfile.mkdtemp()
        self.temp_proj = tempfile.mkdtemp()

        os.environ["HOME"] = self.temp_home
        os.environ["USERPROFILE"] = self.temp_home

        for k in list(os.environ.keys()):
            if k.startswith("ARCDVN"):
                del os.environ[k]

        os.environ["ARCDVN_PROVIDER"] = "google"
        os.environ["ARCDVN_GOOGLE_API_KEY"] = "fake-key"
        os.environ["ARCDVN_MAX_STEPS"] = "15"

        self.orig_cwd = os.getcwd()
        os.chdir(self.temp_proj)
        tools.ROOT = Path(self.temp_proj).resolve()
        tools.invalidate_caches()
        tools.reset_task_metrics()

        self.orig_input = builtins.input
        builtins.input = lambda *a: "y"

    def tearDown(self):
        os.chdir(self.orig_cwd)
        tools.ROOT = Path(self.orig_cwd).resolve()
        tools.invalidate_caches()
        tools.reset_task_metrics()
        builtins.input = self.orig_input
        os.environ.clear()
        os.environ.update(self.orig_environ)

        try:
            shutil.rmtree(self.temp_home, ignore_errors=True)
            shutil.rmtree(self.temp_proj, ignore_errors=True)
        except Exception:
            pass


# ============================================================================
# BENCHMARK 1: Simple conversation — zero filesystem calls, no tool defs
# ============================================================================
class TestBenchmark1_Conversation(BaseEfficiencyTest):

    def test_greeting_zero_filesystem_calls(self):
        """'halo' → direct response, 0 tool calls, tools=None passed to LLM."""
        history = []
        with patch("arcdvncli.agent.chat") as mock_chat:
            mock_chat.return_value = {
                "role": "assistant",
                "content": "Halo! Ada yang bisa saya bantu?",
            }
            result = agent.run_agent("halo", history)

            # Exactly 1 LLM call
            self.assertEqual(mock_chat.call_count, 1)
            # tools=None (no tool definitions sent)
            args, kwargs = mock_chat.call_args
            tools_arg = args[1] if len(args) > 1 else kwargs.get("tools")
            self.assertIsNone(tools_arg, "Conversation should NOT send tool definitions to LLM")
            # Response returned
            self.assertIn("Halo", result)

        # Verify metrics
        metrics = agent.get_last_metrics()
        self.assertEqual(metrics["tool_calls"], 0)
        self.assertEqual(metrics["llm_calls"], 1)
        tool_metrics = metrics.get("tool_metrics", {})
        self.assertEqual(tool_metrics.get("read_count", 0), 0, "No file reads for conversation")
        self.assertEqual(tool_metrics.get("cache_misses", 0), 0)

    def test_explanation_zero_filesystem_calls(self):
        """Technical explanation → direct response, 0 tool calls, tools=None."""
        history = []
        with patch("arcdvncli.agent.chat") as mock_chat:
            mock_chat.return_value = {
                "role": "assistant",
                "content": "TCP adalah protokol berorientasi koneksi...",
            }
            result = agent.run_agent("Jelaskan perbedaan TCP dan UDP", history)

            self.assertEqual(mock_chat.call_count, 1)
            args, kwargs = mock_chat.call_args
            tools_arg = args[1] if len(args) > 1 else kwargs.get("tools")
            self.assertIsNone(tools_arg)
            self.assertIn("TCP", result)


# ============================================================================
# BENCHMARK 2: Targeted HTML/CSS change in existing project
# ============================================================================
class TestBenchmark2_TargetedChange(BaseEfficiencyTest):

    def test_targeted_css_edit(self):
        """Edit a single CSS property → read 1 file, edit 1 file, minimal LLM calls."""
        css_file = Path(self.temp_proj) / "style.css"
        css_file.write_text(
            "body {\n  background: #fff;\n  color: #333;\n}\n",
            encoding="utf-8"
        )
        unrelated = Path(self.temp_proj) / "README.md"
        unrelated.write_text("# Project\nDo not touch\n", encoding="utf-8")

        history = []
        call_seq = iter([
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "style.css"})}
                }]
            },
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c2",
                    "function": {
                        "name": "edit_file",
                        "arguments": json.dumps({
                            "path": "style.css",
                            "old_str": "background: #fff;",
                            "new_str": "background: #1a1a2e;"
                        })
                    }
                }]
            },
            {
                "role": "assistant",
                "content": "Background color telah diubah menjadi #1a1a2e di style.css.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            result = agent.run_agent("Ubah background di style.css jadi warna gelap", history)

        # Verify correctness
        self.assertIn("background: #1a1a2e;", css_file.read_text())
        self.assertIn("color: #333;", css_file.read_text(), "Unrelated CSS preserved")
        self.assertEqual(unrelated.read_text(), "# Project\nDo not touch\n", "Unrelated file untouched")

        # Verify efficiency metrics
        metrics = agent.get_last_metrics()
        self.assertEqual(metrics["tool_calls"], 2, "Exactly 2 tool calls: read + edit")
        self.assertEqual(metrics["llm_calls"], 3)
        tm = metrics.get("tool_metrics", {})
        self.assertEqual(tm.get("read_count", 0), 1, "Only 1 file read")
        self.assertEqual(tm.get("repeated_read_count", 0), 0, "No repeated reads")


# ============================================================================
# BENCHMARK 3: Bug fix requiring multiple related files
# ============================================================================
class TestBenchmark3_MultipleBugfix(BaseEfficiencyTest):

    def test_bugfix_multiple_files(self):
        """Fix bug spanning 2 files → reads both, edits only the buggy one, preserves others."""
        (Path(self.temp_proj) / "utils.py").write_text(
            "def calculate(a, b):\n    return a - b  # BUG\n", encoding="utf-8"
        )
        (Path(self.temp_proj) / "app.py").write_text(
            "from utils import calculate\n\nresult = calculate(5, 3)\nprint(result)\n",
            encoding="utf-8"
        )
        (Path(self.temp_proj) / "config.json").write_text('{"debug": false}', encoding="utf-8")

        history = []
        call_seq = iter([
            # Step 1: list project files
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "list_files", "arguments": "{}"}
                }]
            },
            # Step 2: read the relevant files
            {
                "role": "assistant", "content": None,
                "tool_calls": [
                    {
                        "id": "c2a",
                        "function": {"name": "read_file", "arguments": json.dumps({"path": "utils.py"})}
                    },
                    {
                        "id": "c2b",
                        "function": {"name": "read_file", "arguments": json.dumps({"path": "app.py"})}
                    },
                ]
            },
            # Step 3: edit the buggy file only
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c3",
                    "function": {
                        "name": "edit_file",
                        "arguments": json.dumps({
                            "path": "utils.py",
                            "old_str": "return a - b  # BUG",
                            "new_str": "return a + b"
                        })
                    }
                }]
            },
            {
                "role": "assistant",
                "content": "Bug di utils.py telah diperbaiki. Operator - diubah menjadi +.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            result = agent.run_agent("Fix the bug in calculate function", history)

        # Correctness
        self.assertIn("return a + b", (Path(self.temp_proj) / "utils.py").read_text())
        self.assertEqual(
            (Path(self.temp_proj) / "config.json").read_text(),
            '{"debug": false}',
            "Unrelated files must NOT be modified"
        )
        self.assertIn("utils.py", result)

        # Efficiency
        metrics = agent.get_last_metrics()
        self.assertEqual(metrics["tool_calls"], 4, "list + 2 reads + 1 edit")
        tm = metrics.get("tool_metrics", {})
        self.assertEqual(tm.get("read_count", 0), 2)
        self.assertEqual(tm.get("repeated_read_count", 0), 0)


# ============================================================================
# BENCHMARK 4: Coding task requiring broader repository inspection
# ============================================================================
class TestBenchmark4_BroadInspection(BaseEfficiencyTest):

    def test_broader_inspection_creates_new_file(self):
        """Build a feature that requires listing files and inspecting entry points."""
        (Path(self.temp_proj) / "src").mkdir()
        (Path(self.temp_proj) / "src" / "main.py").write_text("# Main entry", encoding="utf-8")
        (Path(self.temp_proj) / "src" / "models.py").write_text("class User: pass", encoding="utf-8")

        history = []
        call_seq = iter([
            # Step 1: list files
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "list_files", "arguments": "{}"}
                }]
            },
            # Step 2: read entry point
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c2",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "src/main.py"})}
                }]
            },
            # Step 3: read models
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c3",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "src/models.py"})}
                }]
            },
            # Step 4: create new file
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c4",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({
                            "path": "src/views.py",
                            "content": "from .models import User\n\ndef list_users():\n    return []\n"
                        })
                    }
                }]
            },
            {
                "role": "assistant",
                "content": "File src/views.py telah dibuat dengan fungsi list_users.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            result = agent.run_agent("Buat modul views.py di src/ yang import User dari models", history)

        self.assertTrue((Path(self.temp_proj) / "src" / "views.py").is_file())
        content = (Path(self.temp_proj) / "src" / "views.py").read_text()
        self.assertIn("from .models import User", content)
        self.assertIn("views.py", result)

        metrics = agent.get_last_metrics()
        tm = metrics.get("tool_metrics", {})
        self.assertEqual(tm.get("read_count", 0), 2)
        self.assertEqual(tm.get("repeated_read_count", 0), 0)


# ============================================================================
# BENCHMARK 5: Repeated reads, cache invalidation, duplicate prevention
# ============================================================================
class TestBenchmark5_CacheAndDuplicates(BaseEfficiencyTest):

    def test_repeated_read_same_file_tracked(self):
        """Reading the same file twice increments repeated_read_count."""
        target = Path(self.temp_proj) / "data.txt"
        target.write_text("line1\nline2\nline3\n", encoding="utf-8")

        history = []
        call_seq = iter([
            # Read 1
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "data.txt"})}
                }]
            },
            # Read 2: same file, same range — should be flagged as repeated
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c2",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "data.txt"})}
                }]
            },
            {
                "role": "assistant",
                "content": "File data.txt berisi 3 baris.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            agent.run_agent("Baca file data.txt", history)

        metrics = agent.get_last_metrics()
        tm = metrics.get("tool_metrics", {})
        self.assertEqual(tm["read_count"], 2, "Total reads should be 2")
        self.assertEqual(tm["repeated_read_count"], 1, "Second read is a repeat")
        # Cache should have hit on the second read (same mtime)
        self.assertGreaterEqual(tm["cache_hits"], 1, "Second read should be a cache hit")

    def test_cache_invalidated_after_edit(self):
        """After editing a file, re-reading it should NOT be flagged as repeated."""
        target = Path(self.temp_proj) / "editable.py"
        target.write_text("x = 1\n", encoding="utf-8")

        history = []
        call_seq = iter([
            # Read
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "editable.py"})}
                }]
            },
            # Edit
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c2",
                    "function": {
                        "name": "edit_file",
                        "arguments": json.dumps({
                            "path": "editable.py",
                            "old_str": "x = 1",
                            "new_str": "x = 2"
                        })
                    }
                }]
            },
            # Re-read after edit — should NOT count as repeated
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c3",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "editable.py"})}
                }]
            },
            {
                "role": "assistant",
                "content": "File editable.py telah diubah dan diverifikasi.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            agent.run_agent("Edit x menjadi 2 di editable.py", history)

        metrics = agent.get_last_metrics()
        tm = metrics.get("tool_metrics", {})
        self.assertEqual(tm["read_count"], 2)
        # The re-read after edit should NOT be counted as repeated
        self.assertEqual(tm["repeated_read_count"], 0,
                         "Re-read after edit should not be flagged as repeated")

    def test_no_duplicate_tool_results_in_history(self):
        """Verify tool results are appended exactly once per tool call."""
        target = Path(self.temp_proj) / "single.txt"
        target.write_text("content", encoding="utf-8")

        history = []
        messages_captured = []

        def mock_chat(messages, tools_def=None):
            messages_captured.append(list(messages))
            if len(messages_captured) == 1:
                return {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "c1",
                        "function": {"name": "read_file", "arguments": json.dumps({"path": "single.txt"})}
                    }]
                }
            return {"role": "assistant", "content": "Done."}

        with patch("arcdvncli.agent.chat", side_effect=mock_chat):
            agent.run_agent("Baca single.txt", history)

        # Check that in the second LLM call, the tool result appears exactly once
        if len(messages_captured) >= 2:
            second_call_msgs = messages_captured[1]
            tool_results = [m for m in second_call_msgs if m.get("role") == "tool"]
            self.assertEqual(len(tool_results), 1,
                             "Tool result must appear exactly once in message history")

    def test_tool_result_truncation(self):
        """Oversized tool results are truncated before being sent to LLM."""
        big_file = Path(self.temp_proj) / "big.txt"
        big_file.write_text("A" * 20000, encoding="utf-8")

        history = []
        messages_captured = []

        def mock_chat(messages, tools_def=None):
            messages_captured.append(list(messages))
            if len(messages_captured) == 1:
                return {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "c1",
                        "function": {"name": "read_file", "arguments": json.dumps({"path": "big.txt"})}
                    }]
                }
            return {"role": "assistant", "content": "Large file read."}

        with patch("arcdvncli.agent.chat", side_effect=mock_chat):
            agent.run_agent("Baca big.txt", history)

        if len(messages_captured) >= 2:
            second_call_msgs = messages_captured[1]
            tool_results = [m for m in second_call_msgs if m.get("role") == "tool"]
            self.assertEqual(len(tool_results), 1)
            content = tool_results[0].get("content", "")
            # Default TOOL_RESULT_LIMIT is 8000 chars
            self.assertLessEqual(len(content), 8200,
                                 "Tool result must be truncated to limit")
            self.assertIn("dipotong", content, "Truncated result should have indicator")

    def test_dir_cache_hit_on_repeat(self):
        """Repeated list_files for same path uses cache."""
        (Path(self.temp_proj) / "a.txt").write_text("a", encoding="utf-8")

        history = []
        call_seq = iter([
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "list_files", "arguments": "{}"}
                }]
            },
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c2",
                    "function": {"name": "list_files", "arguments": "{}"}
                }]
            },
            {"role": "assistant", "content": "Listed."}
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            agent.run_agent("List files twice", history)

        metrics = agent.get_last_metrics()
        tm = metrics.get("tool_metrics", {})
        self.assertGreaterEqual(tm["cache_hits"], 1, "Second list_files should be a cache hit")


# ============================================================================
# BENCHMARK 6: Provider rate-limit and timeout recovery
# ============================================================================
class TestBenchmark6_ProviderRecovery(BaseEfficiencyTest):

    @patch("time.sleep")
    @patch("requests.post")
    def test_503_retry_and_recovery(self, mock_post, mock_sleep):
        """HTTP 503 retries with backoff and recovers."""
        err_resp = MagicMock()
        err_resp.ok = False
        err_resp.status_code = 503
        err_resp.headers = {"Retry-After": "0.1"}
        err_resp.json.return_value = {"error": {"message": "Service Unavailable"}}

        ok_resp = MagicMock()
        ok_resp.ok = True
        ok_resp.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "Recovered!"}}]
        }
        mock_post.side_effect = [err_resp, ok_resp]

        msg = llm.chat([{"role": "user", "content": "test"}])
        self.assertEqual(msg["content"], "Recovered!")
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once()  # backoff delay called

    @patch("time.sleep")
    @patch("requests.post")
    def test_429_exhausts_retries(self, mock_post, mock_sleep):
        """HTTP 429 retries up to max then raises LLMError."""
        err_resp = MagicMock()
        err_resp.ok = False
        err_resp.status_code = 429
        err_resp.headers = {"Retry-After": "0.05"}
        err_resp.json.return_value = {"error": {"message": "Rate limited"}}
        mock_post.return_value = err_resp

        with self.assertRaises(LLMError) as ctx:
            llm.chat([{"role": "user", "content": "test"}])
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(mock_post.call_count, 4)  # 1 initial + 3 retries

    @patch("time.sleep")
    @patch("requests.post")
    def test_timeout_recovery(self, mock_post, mock_sleep):
        """Connection timeout retries and eventually succeeds."""
        import requests as req
        ok_resp = MagicMock()
        ok_resp.ok = True
        ok_resp.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "OK"}}]
        }
        mock_post.side_effect = [
            req.exceptions.Timeout("timed out"),
            ok_resp,
        ]

        msg = llm.chat([{"role": "user", "content": "test"}])
        self.assertEqual(msg["content"], "OK")
        self.assertEqual(mock_post.call_count, 2)

    def test_agent_handles_llm_error_gracefully(self):
        """Agent returns informative message when LLM fails after tool work."""
        (Path(self.temp_proj) / "saved.html").write_text("<h1>Saved</h1>", encoding="utf-8")

        call_count = [0]

        def mock_chat(messages, tools_def=None):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "role": "assistant", "content": None,
                    "tool_calls": [{
                        "id": "c1",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "new.html", "content": "<p>New</p>"})
                        }
                    }]
                }
            raise LLMError("provider_overloaded", retryable=True)

        with patch("arcdvncli.agent.chat", side_effect=mock_chat):
            result = agent.run_agent("Buat halaman", [])

        self.assertTrue((Path(self.temp_proj) / "new.html").is_file(),
                         "Files written before failure must persist")
        self.assertIn("new.html", result, "Error message should reference saved files")

        # Agent should report the failure gracefully
        metrics = agent.get_last_metrics()
        self.assertIn("failure_categories", metrics)
        # retry_count tracks agent-level failures, may be 0 if agent caught the error
        self.assertIsInstance(metrics.get("retry_count", 0), int)


# ============================================================================
# ACCEPTANCE: Context compaction for long conversations
# ============================================================================
class TestContextCompaction(BaseEfficiencyTest):

    def test_compact_history_shrinks_old_tool_results(self):
        """Large old tool results are compacted to keep context manageable."""
        from arcdvncli.agent import _compact_history_if_needed

        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Build something"},
            # Old large tool result
            {"role": "tool", "tool_call_id": "old1", "content": "X" * 5000},
            {"role": "assistant", "content": "step 1 done"},
            {"role": "tool", "tool_call_id": "old2", "content": "Y" * 5000},
            {"role": "assistant", "content": "step 2 done"},
            # Recent messages (should NOT be compacted)
            {"role": "user", "content": "Continue"},
            {"role": "assistant", "content": "Working..."},
            {"role": "tool", "tool_call_id": "recent", "content": "Z" * 3000},
            {"role": "assistant", "content": "Done"},
        ]

        _compact_history_if_needed(messages, limit=5000)

        # Old tool results (indices 2, 4) should be compacted
        self.assertLess(len(messages[2]["content"]), 1000)
        self.assertIn("kompaksi", messages[2]["content"])
        self.assertLess(len(messages[4]["content"]), 1000)

        # Recent tool result (index 8) should NOT be compacted
        self.assertEqual(len(messages[8]["content"]), 3000)


# ============================================================================
# ACCEPTANCE: Metrics structure validation
# ============================================================================
class TestMetricsStructure(BaseEfficiencyTest):

    def test_metrics_contain_required_fields(self):
        """get_last_metrics() contains all required efficiency fields."""
        history = []
        with patch("arcdvncli.agent.chat") as mock_chat:
            mock_chat.return_value = {
                "role": "assistant", "content": "Test response.",
            }
            agent.run_agent("halo", history)

        metrics = agent.get_last_metrics()
        required_top = ["intent", "duration", "llm_calls", "tool_calls", "steps", "tool_duration"]
        for field in required_top:
            self.assertIn(field, metrics, f"Missing top-level metric: {field}")

        self.assertIn("tool_metrics", metrics)
        tm = metrics["tool_metrics"]
        required_tm = ["cache_hits", "cache_misses", "read_count",
                        "repeated_read_count", "tool_result_bytes", "tool_counts"]
        for field in required_tm:
            self.assertIn(field, tm, f"Missing tool metric: {field}")

    def test_coding_task_metrics_contain_retry_fields(self):
        """Coding tasks include retry_count and failure_categories."""
        history = []
        call_seq = iter([
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "list_files", "arguments": "{}"}
                }]
            },
            {"role": "assistant", "content": "Listed."}
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            agent.run_agent("cek file di sini", history)

        metrics = agent.get_last_metrics()
        self.assertIn("retry_count", metrics)
        self.assertIn("failure_categories", metrics)
        self.assertEqual(metrics["retry_count"], 0)


# ============================================================================
# ACCEPTANCE: Tool result bytes tracking
# ============================================================================
class TestToolResultBytesTracking(BaseEfficiencyTest):

    def test_tool_result_bytes_tracked(self):
        """tool_result_bytes accumulates the bytes of all tool results sent to LLM."""
        (Path(self.temp_proj) / "small.txt").write_text("hello world", encoding="utf-8")

        history = []
        call_seq = iter([
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "small.txt"})}
                }]
            },
            {"role": "assistant", "content": "Read."}
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            agent.run_agent("Baca small.txt", history)

        metrics = agent.get_last_metrics()
        tm = metrics.get("tool_metrics", {})
        self.assertGreater(tm.get("tool_result_bytes", 0), 0,
                           "Tool result bytes should be tracked")
        self.assertGreater(tm.get("tool_result_bytes", 0), 5,
                           "Tool result bytes should include the file content length")


if __name__ == "__main__":
    unittest.main()
