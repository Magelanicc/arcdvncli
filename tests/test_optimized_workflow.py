"""
Tests for Phase 7: Optimized ARCDVNCLI Workflow & Scenarios (1-8).
Verifies:
1. 'halo' -> direct conversational response, no list_files, no tool loop.
2. 'Jelaskan perbedaan TCP dan UDP' -> direct explanation without project scanning.
3. 'Cek struktur project ini' -> relevant file inspection.
4. 'Buat landing page sederhana dengan HTML, CSS, dan JavaScript' -> files created on disk.
5. 'Perbaiki bug di file yang relevan' -> inspects, edits with edit_file, preserves others.
6. Simulated HTTP 429 or 503 -> bounded retries and understandable status.
7. Empty model response or malformed tool call -> graceful recovery / clear error.
8. Repeated tool call with no progress -> cycle/stall detection aborts safely.
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

from arcdvncli import agent, tools, llm
from arcdvncli.router import detect_intent, TaskIntent
from arcdvncli.llm import LLMError


class TestOptimizedWorkflow(unittest.TestCase):

    def setUp(self):
        self.orig_environ = dict(os.environ)
        self.temp_home = tempfile.mkdtemp()
        self.temp_proj = tempfile.mkdtemp()

        os.environ["HOME"] = self.temp_home
        os.environ["USERPROFILE"] = self.temp_home
        os.environ["ARCDVN_API_KEY"] = "sk-test-key"

        self.orig_cwd = os.getcwd()
        os.chdir(self.temp_proj)
        tools.ROOT = Path(self.temp_proj).resolve()
        tools.invalidate_caches()

        self.orig_input = builtins.input
        builtins.input = lambda *a: "y"

    def tearDown(self):
        os.chdir(self.orig_cwd)
        tools.ROOT = Path(self.orig_cwd).resolve()
        tools.invalidate_caches()
        builtins.input = self.orig_input
        os.environ.clear()
        os.environ.update(self.orig_environ)
        shutil.rmtree(self.temp_home, ignore_errors=True)
        shutil.rmtree(self.temp_proj, ignore_errors=True)

    def test_scenario_1_greeting_direct_response(self):
        """Scenario 1: 'halo' gets direct conversational response, no list_files, 1 LLM request."""
        history = []
        intent = detect_intent("halo")
        self.assertEqual(intent, TaskIntent.CONVERSATION)

        with patch("arcdvncli.agent.chat") as mock_chat:
            mock_chat.return_value = {
                "role": "assistant",
                "content": "Halo! Ada yang bisa saya bantu hari ini?",
            }
            res = agent.run_agent("halo", history)

            # Verification:
            # 1. Exactly 1 LLM call
            self.assertEqual(mock_chat.call_count, 1)
            # 2. tools passed to chat was None (direct response, no function calling schema overhead)
            _, kwargs = mock_chat.call_args
            tools_arg = mock_chat.call_args[0][1] if len(mock_chat.call_args[0]) > 1 else kwargs.get("tools")
            self.assertIsNone(tools_arg)
            # 3. Response is returned directly
            self.assertIn("Halo! Ada yang bisa saya bantu", res)
            # 4. History updated
            self.assertEqual(len(history), 2)
            # 5. Metrics recorded
            metrics = agent.get_last_metrics()
            self.assertEqual(metrics["intent"], TaskIntent.CONVERSATION)
            self.assertEqual(metrics["tool_calls"], 0)
            self.assertEqual(metrics["llm_calls"], 1)

    def test_scenario_2_explanation_direct_response(self):
        """Scenario 2: 'Jelaskan perbedaan TCP dan UDP' answers directly without project inspection."""
        history = []
        user_input = "Jelaskan perbedaan TCP dan UDP"
        intent = detect_intent(user_input)
        self.assertEqual(intent, TaskIntent.EXPLANATION)

        with patch("arcdvncli.agent.chat") as mock_chat:
            mock_chat.return_value = {
                "role": "assistant",
                "content": "TCP adalah protokol berorientasi koneksi yang andal, sedangkan UDP bersifat connectionless dan cepat.",
            }
            res = agent.run_agent(user_input, history)

            # 1 LLM request, 0 tools
            self.assertEqual(mock_chat.call_count, 1)
            tools_arg = mock_chat.call_args[0][1] if len(mock_chat.call_args[0]) > 1 else mock_chat.call_args[1].get("tools")
            self.assertIsNone(tools_arg)
            self.assertIn("TCP adalah protokol", res)
            metrics = agent.get_last_metrics()
            self.assertEqual(metrics["intent"], TaskIntent.EXPLANATION)
            self.assertEqual(metrics["tool_calls"], 0)

    def test_scenario_3_project_inspection(self):
        """Scenario 3: 'Cek struktur project ini' performs relevant file inspection via list_files."""
        # Create some files
        (Path(self.temp_proj) / "src").mkdir()
        (Path(self.temp_proj) / "src" / "main.py").write_text("print('ok')", encoding="utf-8")
        (Path(self.temp_proj) / "README.md").write_text("# Project", encoding="utf-8")

        history = []
        user_input = "Cek struktur project ini"
        intent = detect_intent(user_input)
        self.assertEqual(intent, TaskIntent.INSPECTION)

        call_seq = iter([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "list_files", "arguments": "{}"}
                }]
            },
            {
                "role": "assistant",
                "content": "Struktur project terdiri dari README.md dan src/main.py.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq) as mock_chat:
            res = agent.run_agent(user_input, history)
            self.assertIn("Struktur project", res)
            self.assertEqual(mock_chat.call_count, 2)
            metrics = agent.get_last_metrics()
            self.assertEqual(metrics["intent"], TaskIntent.INSPECTION)
            self.assertEqual(metrics["tool_calls"], 1)

    def test_scenario_4_coding_landing_page(self):
        """Scenario 4: 'Buat landing page sederhana...' creates actual files on disk."""
        history = []
        user_input = "Buat landing page sederhana dengan HTML, CSS, dan JavaScript"
        intent = detect_intent(user_input)
        self.assertEqual(intent, TaskIntent.CODING)

        call_seq = iter([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c_html",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "index.html",
                                "content": "<!DOCTYPE html><html><head><link rel='stylesheet' href='style.css'></head><body><h1>Landing</h1><script src='app.js'></script></body></html>"
                            })
                        }
                    },
                    {
                        "id": "c_css",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "style.css",
                                "content": "body { font-family: sans-serif; background: #f0f0f0; }"
                            })
                        }
                    },
                    {
                        "id": "c_js",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "app.js",
                                "content": "console.log('Landing page initialized');"
                            })
                        }
                    }
                ]
            },
            {
                "role": "assistant",
                "content": "Landing page sederhana telah berhasil dibuat dengan file index.html, style.css, dan app.js.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            res = agent.run_agent(user_input, history)

        html_p = Path(self.temp_proj) / "index.html"
        css_p = Path(self.temp_proj) / "style.css"
        js_p = Path(self.temp_proj) / "app.js"

        self.assertTrue(html_p.is_file())
        self.assertTrue(css_p.is_file())
        self.assertTrue(js_p.is_file())
        self.assertIn("index.html", res)
        self.assertIn("style.css", res)
        self.assertIn("app.js", res)

    def test_scenario_5_bugfix_in_relevant_file(self):
        """Scenario 5: 'Perbaiki bug di file yang relevan' reads and edits target file accurately."""
        app_file = Path(self.temp_proj) / "calc.py"
        app_file.write_text("def add(a, b):\n    return a - b  # BUG: should be +\n", encoding="utf-8")
        unrelated = Path(self.temp_proj) / "notes.txt"
        unrelated.write_text("Don't touch me\n", encoding="utf-8")

        history = []
        user_input = "Perbaiki bug di file yang relevan"
        intent = detect_intent(user_input)
        self.assertEqual(intent, TaskIntent.CODING)

        call_seq = iter([
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "c_read",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "calc.py"})}
                }]
            },
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "c_edit",
                    "function": {
                        "name": "edit_file",
                        "arguments": json.dumps({
                            "path": "calc.py",
                            "old_str": "return a - b  # BUG: should be +",
                            "new_str": "return a + b"
                        })
                    }
                }]
            },
            {
                "role": "assistant",
                "content": "Bug pada fungsi add di calc.py telah diperbaiki.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            res = agent.run_agent(user_input, history)

        self.assertIn("return a + b", app_file.read_text())
        self.assertEqual(unrelated.read_text(), "Don't touch me\n")
        self.assertIn("calc.py", res)

    def test_scenario_6_simulated_retry_and_feedback(self):
        """Scenario 6: Simulated 429/503 bounded retry behavior with retry-after."""
        # 1. Test 503 retryable
        mock_resp_503 = MagicMock()
        mock_resp_503.status_code = 503
        mock_resp_503.ok = False
        mock_resp_503.headers = {"Retry-After": "0.1"}
        mock_resp_503.json.return_value = {"error": {"message": "Service Unavailable"}}

        mock_resp_success = MagicMock()
        mock_resp_success.status_code = 200
        mock_resp_success.ok = True
        mock_resp_success.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "Pulih dari 503"}}]
        }

        with patch("requests.post", side_effect=[mock_resp_503, mock_resp_success]):
            msg = llm.chat([{"role": "user", "content": "ping"}])
            self.assertEqual(msg.get("content"), "Pulih dari 503")

        # 2. Test 429 max retries exhausted
        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        mock_resp_429.ok = False
        mock_resp_429.headers = {"Retry-After": "0.05"}
        mock_resp_429.json.return_value = {"error": {"message": "Rate limit exceeded"}}

        with patch("requests.post", return_value=mock_resp_429):
            with self.assertRaises(LLMError) as ctx:
                llm.chat([{"role": "user", "content": "ping"}])
            self.assertTrue(ctx.exception.retryable)
            self.assertIn("429", str(ctx.exception))

    def test_scenario_7_empty_response_recovery(self):
        """Scenario 7: Empty model response recovers gracefully on nudge."""
        history = []
        call_seq = iter([
            {"role": "assistant", "content": "   "},  # whitespace empty
            {"role": "assistant", "content": "Respons pulih berhasil."},
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            res = agent.run_agent("halo", history)

        self.assertIn("Respons pulih berhasil", res)

    def test_scenario_8_repeated_tool_call_loop_detection(self):
        """Scenario 8: Repeated unproductive tool call aborts safely with cycle warning."""
        history = []
        stalling_call = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "c_stuck",
                "function": {"name": "list_files", "arguments": "{}"}
            }]
        }

        with patch("arcdvncli.agent.chat", return_value=stalling_call):
            res = agent.run_agent("Cek struktur file", history)

        self.assertIn("loop", res.lower())
        self.assertIn("mencegah pemborosan", res.lower())


if __name__ == "__main__":
    unittest.main()
