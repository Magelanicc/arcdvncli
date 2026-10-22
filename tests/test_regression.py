"""
Comprehensive regression test suite for ARCDVNCLI.
Tests Configuration, LLM Client, Agent Loop, End-to-End Scenarios, and Security.

Can be run via:
    python -m unittest discover -s tests -v
or:
    python tests/test_regression.py
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
from unittest.mock import MagicMock, patch

import requests

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

for _s in (sys.stdout, sys.stderr):
    try:
        if hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from arcdvncli import agent, cli, llm, tools
from arcdvncli.llm import LLMError, PROVIDERS


class BaseIsolatedTest(unittest.TestCase):
    """Base test class providing an isolated temporary HOME and project directory."""

    def setUp(self):
        self.orig_environ = dict(os.environ)
        self.temp_home = tempfile.mkdtemp()
        self.temp_proj = tempfile.mkdtemp()

        os.environ["HOME"] = self.temp_home
        os.environ["USERPROFILE"] = self.temp_home

        # Clear any ambient ARCDVN_* variables
        for k in list(os.environ.keys()):
            if k.startswith("ARCDVN"):
                del os.environ[k]

        self.orig_cwd = os.getcwd()
        os.chdir(self.temp_proj)
        tools.ROOT = Path(self.temp_proj).resolve()

        # By default auto-confirm tool executions in tests
        self.orig_input = builtins.input
        builtins.input = lambda *a: "y"

    def tearDown(self):
        os.chdir(self.orig_cwd)
        tools.ROOT = Path(self.orig_cwd).resolve()
        builtins.input = self.orig_input
        os.environ.clear()
        os.environ.update(self.orig_environ)

        try:
            shutil.rmtree(self.temp_home, ignore_errors=True)
            shutil.rmtree(self.temp_proj, ignore_errors=True)
        except Exception:
            pass


# ============================================================================
# A. CONFIGURATION TESTS
# ============================================================================
class TestConfiguration(BaseIsolatedTest):

    def test_fresh_installation_defaults(self):
        """A new install without config defaults to Google Gemini and gemini-3.8-flash."""
        prov = llm.get_active_provider()
        self.assertEqual(prov, "google")
        cfg = llm.get_provider_config()
        self.assertEqual(cfg["id"], "google")
        self.assertEqual(cfg["model"], "gemini-3.8-flash")
        self.assertEqual(cfg["base_url"], "https://generativelanguage.googleapis.com/v1beta/openai")

    def test_existing_openrouter_migration(self):
        """Legacy ARCDVN_API_KEY with OpenRouter model/url is migrated safely without data loss."""
        cfg_file = llm.get_config_path()
        Path(cfg_file).write_text(
            "ARCDVN_API_KEY=sk-or-legacy-key\n"
            "ARCDVN_MODEL=google/gemini-2.5-flash\n",
            encoding="utf-8"
        )
        llm.load_env()

        active = llm.get_active_provider()
        self.assertEqual(active, "openrouter")
        self.assertEqual(os.environ.get("ARCDVN_OPENROUTER_API_KEY"), "sk-or-legacy-key")
        self.assertEqual(os.environ.get("ARCDVN_API_KEY"), "sk-or-legacy-key")

    def test_provider_switching_preserves_both_credentials(self):
        """Switching between Google and OpenRouter retains both credentials."""
        llm.save_setting("ARCDVN_PROVIDER", "google")
        llm.save_setting("ARCDVN_GOOGLE_API_KEY", "google-secret-key")
        llm.save_setting("ARCDVN_OPENROUTER_API_KEY", "openrouter-secret-key")

        # Active is Google
        cfg_google = llm.get_provider_config()
        self.assertEqual(cfg_google["id"], "google")
        self.assertEqual(cfg_google["key"], "google-secret-key")

        # Switch to OpenRouter
        llm.save_setting("ARCDVN_PROVIDER", "openrouter")
        cfg_or = llm.get_provider_config()
        self.assertEqual(cfg_or["id"], "openrouter")
        self.assertEqual(cfg_or["key"], "openrouter-secret-key")

        # Switch back to Google
        llm.save_setting("ARCDVN_PROVIDER", "google")
        cfg_google2 = llm.get_provider_config()
        self.assertEqual(cfg_google2["key"], "google-secret-key")

    def test_local_env_cannot_inject_or_override_api_keys(self):
        """Security: Project-local .env cannot override global API keys or inject credentials."""
        global_cfg = llm.get_config_path()
        Path(global_cfg).write_text("ARCDVN_GOOGLE_API_KEY=legit-google-key\n", encoding="utf-8")

        local_env = Path(self.temp_proj) / ".env"
        local_env.write_text(
            "ARCDVN_GOOGLE_API_KEY=malicious-key\n"
            "ARCDVN_API_KEY=evil-key\n"
            "ARCDVN_OPENROUTER_API_KEY=rogue-key\n"
            "ARCDVN_SAFE_PARAM=custom-value\n",
            encoding="utf-8"
        )

        llm.load_env()
        self.assertEqual(os.environ.get("ARCDVN_GOOGLE_API_KEY"), "legit-google-key")
        self.assertNotEqual(os.environ.get("ARCDVN_API_KEY"), "evil-key")
        self.assertNotEqual(os.environ.get("ARCDVN_OPENROUTER_API_KEY"), "rogue-key")

    def test_secret_redaction(self):
        """Secret redaction sanitizes API keys and tokens from strings."""
        mock_google_key = "mock-secret-google-token-xyz123"
        mock_or_key = "mock-secret-openrouter-token-xyz789"
        os.environ["ARCDVN_GOOGLE_API_KEY"] = mock_google_key
        os.environ["ARCDVN_OPENROUTER_API_KEY"] = mock_or_key

        # Construct token pattern dynamically so GitHub secret scanner does not trip on fake data
        dummy_pattern = "".join(["s", "k", "-or-v1-"]) + ("0123456789abcdef" * 4)
        raw = f"Error in https://api.com?key={mock_google_key} with token {mock_or_key} and raw {dummy_pattern}"
        redacted = llm.redact_secrets(raw)

        self.assertNotIn(mock_google_key, redacted)
        self.assertNotIn(mock_or_key, redacted)
        self.assertNotIn("0123456789abcdef", redacted)
        self.assertIn("[REDACTED", redacted)


# ============================================================================
# B. LLM CLIENT TESTS
# ============================================================================
class TestLLMClient(BaseIsolatedTest):

    def setUp(self):
        super().setUp()
        os.environ["ARCDVN_PROVIDER"] = "google"
        os.environ["ARCDVN_GOOGLE_API_KEY"] = "fake-key"
        os.environ["ARCDVN_MODEL"] = "gemini-3.8-flash"

    def test_missing_api_key_raises_informative_error(self):
        """Missing API key raises LLMError referencing /settings."""
        os.environ.pop("ARCDVN_GOOGLE_API_KEY", None)
        os.environ.pop("ARCDVN_API_KEY", None)
        with self.assertRaises(LLMError) as ctx:
            llm.chat([{"role": "user", "content": "hi"}])
        self.assertIn("/settings", str(ctx.exception))

    @patch("requests.post")
    def test_valid_text_completion(self, mock_post):
        """Standard valid text completion is returned correctly."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "Halo! Ada yang bisa dibantu?"}}]
        }
        mock_post.return_value = mock_resp

        msg = llm.chat([{"role": "user", "content": "halo"}])
        self.assertEqual(msg["content"], "Halo! Ada yang bisa dibantu?")
        self.assertEqual(msg["role"], "assistant")

    @patch("requests.post")
    def test_valid_tool_call_response(self, mock_post):
        """Valid response with single tool call is preserved."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "list_files", "arguments": "{}"}
                    }]
                }
            }]
        }
        mock_post.return_value = mock_resp

        msg = llm.chat([{"role": "user", "content": "cek file"}], tools=tools.TOOL_DEFS)
        self.assertIn("tool_calls", msg)
        self.assertEqual(len(msg["tool_calls"]), 1)
        self.assertEqual(msg["tool_calls"][0]["function"]["name"], "list_files")

    @patch("requests.post")
    def test_empty_choices_raises_error(self, mock_post):
        """Empty choices in payload raises non-retryable or overload error."""
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"choices": []}
        mock_post.return_value = mock_resp

        with self.assertRaises(LLMError) as ctx:
            llm.chat([{"role": "user", "content": "test"}])
        self.assertIn("choices", str(ctx.exception))

    @patch("requests.post")
    def test_permanent_http_errors_no_retry(self, mock_post):
        """HTTP 401/403/404 raise immediately without retrying."""
        for code in (401, 403, 404):
            mock_resp = MagicMock()
            mock_resp.ok = False
            mock_resp.status_code = code
            mock_resp.json.return_value = {"error": {"message": f"Auth failed with {code}"}}
            mock_post.reset_mock()
            mock_post.return_value = mock_resp

            with self.assertRaises(LLMError) as ctx:
                llm.chat([{"role": "user", "content": "hi"}])
            self.assertFalse(ctx.exception.retryable)
            self.assertEqual(mock_post.call_count, 1)

    @patch("requests.post")
    @patch("time.sleep")
    def test_retryable_http_503_recovers(self, mock_sleep, mock_post):
        """HTTP 503 retries and recovers on success."""
        err_resp = MagicMock()
        err_resp.ok = False
        err_resp.status_code = 503
        err_resp.json.return_value = {"error": {"message": "Service Unavailable"}}

        ok_resp = MagicMock()
        ok_resp.ok = True
        ok_resp.json.return_value = {
            "choices": [{"message": {"role": "assistant", "content": "Recovered!"}}]
        }
        mock_post.side_effect = [err_resp, ok_resp]

        msg = llm.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(msg["content"], "Recovered!")
        self.assertEqual(mock_post.call_count, 2)


# ============================================================================
# C. AGENT LOOP TESTS
# ============================================================================
class TestAgentLoop(BaseIsolatedTest):

    def setUp(self):
        super().setUp()
        os.environ["ARCDVN_PROVIDER"] = "google"
        os.environ["ARCDVN_GOOGLE_API_KEY"] = "fake-key"
        os.environ["ARCDVN_MAX_STEPS"] = "10"

    def test_text_only_response_finishes(self):
        """Text only response finishes cleanly and updates history."""
        history = []
        with patch("arcdvncli.agent.chat") as mock_chat:
            mock_chat.return_value = {
                "role": "assistant",
                "content": "Ini penjelasan mengenai proyek Anda.",
            }
            res = agent.run_agent("Jelaskan proyek", history)

        self.assertIn("Ini penjelasan", res)
        self.assertEqual(len(history), 2)  # user + assistant

    def test_single_tool_execution_continues(self):
        """Single tool call executes, records result, and completes."""
        history = []
        call_seq = iter([
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "write_file", "arguments": json.dumps({"path": "test.txt", "content": "hello"})}
                }]
            },
            {
                "role": "assistant",
                "content": "File test.txt telah berhasil dibuat.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            res = agent.run_agent("Buat test.txt", history)

        self.assertIn("File test.txt telah berhasil dibuat", res)
        self.assertTrue((Path(self.temp_proj) / "test.txt").exists())
        self.assertEqual((Path(self.temp_proj) / "test.txt").read_text(), "hello")

    def test_anti_code_in_terminal_steering(self):
        """When model outputs code in chat without calling write_file, agent steers it."""
        history = []
        call_seq = iter([
            # Turn 1: Model outputs code in chat text instead of calling tools
            {
                "role": "assistant",
                "content": "Tentu, ini kodenya:\n```html\n<!DOCTYPE html><html><body><h1>Landing</h1></body></html>\n```\nSimpan ke index.html.",
            },
            # Turn 2: After steering, model calls write_file
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c2",
                    "function": {"name": "write_file", "arguments": json.dumps({"path": "index.html", "content": "<h1>Landing</h1>"})}
                }]
            },
            # Turn 3: Model finishes
            {
                "role": "assistant",
                "content": "File index.html telah ditulis ke disk.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            res = agent.run_agent("Buat index.html", history)

        self.assertTrue((Path(self.temp_proj) / "index.html").exists())
        self.assertIn("index.html", res)

    def test_stall_detection_on_repeated_calls(self):
        """Repeated identical tool calls trigger loop protection and abort safely."""
        history = []
        stalling_call = {
            "role": "assistant", "content": None,
            "tool_calls": [{
                "id": "c_repeat",
                "function": {"name": "list_files", "arguments": "{}"}
            }]
        }
        with patch("arcdvncli.agent.chat", return_value=stalling_call):
            res = agent.run_agent("Cari file", history)

        self.assertIn("loop", res.lower())

    def test_empty_response_recovery(self):
        """Empty response is nudged and recovers when subsequent response is valid."""
        history = []
        call_seq = iter([
            {"role": "assistant", "content": ""},  # empty
            {"role": "assistant", "content": "Selesai memproses."},
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            res = agent.run_agent("Halo", history)

        self.assertIn("Selesai memproses", res)


# ============================================================================
# D. END-TO-END CODING SCENARIOS
# ============================================================================
class TestEndToEndScenarios(BaseIsolatedTest):

    def test_scenario_1_new_project_landing_page(self):
        """Scenario 1: Creates landing page (HTML, CSS, JS), files exist on disk with content."""
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
            # Step 2: create index.html, style.css, app.js
            {
                "role": "assistant", "content": None,
                "tool_calls": [
                    {
                        "id": "c2_html",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "index.html",
                                "content": "<!DOCTYPE html><html><head><link rel='stylesheet' href='style.css'></head><body><div class='card'>Card</div><script src='app.js'></script></body></html>"
                            })
                        }
                    },
                    {
                        "id": "c2_css",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "style.css",
                                "content": "body { background: linear-gradient(135deg, #1e3c72, #2a5298); }"
                            })
                        }
                    },
                    {
                        "id": "c2_js",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({
                                "path": "app.js",
                                "content": "console.log('Landing page active');"
                            })
                        }
                    }
                ]
            },
            # Step 3: summary
            {
                "role": "assistant",
                "content": "Landing page berhasil dibuat dengan file index.html, style.css, dan app.js.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            res = agent.run_agent(
                "Create a simple responsive landing page using HTML, CSS, and JavaScript. Use a modern blue gradient and neomorphic cards.",
                history
            )

        html_p = Path(self.temp_proj) / "index.html"
        css_p = Path(self.temp_proj) / "style.css"
        js_p = Path(self.temp_proj) / "app.js"

        self.assertTrue(html_p.is_file())
        self.assertTrue(css_p.is_file())
        self.assertTrue(js_p.is_file())
        self.assertGreater(html_p.stat().st_size, 0)
        self.assertGreater(css_p.stat().st_size, 0)
        self.assertGreater(js_p.stat().st_size, 0)
        self.assertIn("style.css", html_p.read_text())
        self.assertIn("app.js", html_p.read_text())

    def test_scenario_2_existing_project_modification(self):
        """Scenario 2: Inspect existing file, modify specific lines via edit_file without overwriting unrelated files."""
        app_file = Path(self.temp_proj) / "app.py"
        app_file.write_text("def compute():\n    version = 1\n    return version * 10\n", encoding="utf-8")
        unrelated = Path(self.temp_proj) / "README.md"
        unrelated.write_text("# Project Info\nDo not change me\n", encoding="utf-8")

        history = []
        call_seq = iter([
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {"name": "read_file", "arguments": json.dumps({"path": "app.py"})}
                }]
            },
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c2",
                    "function": {
                        "name": "edit_file",
                        "arguments": json.dumps({
                            "path": "app.py",
                            "old_str": "version = 1",
                            "new_str": "version = 2"
                        })
                    }
                }]
            },
            {
                "role": "assistant",
                "content": "Nilai version di app.py telah diubah menjadi 2.",
            }
        ])

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            res = agent.run_agent("Update version to 2 in app.py", history)

        self.assertIn("version = 2", app_file.read_text())
        self.assertIn("return version * 10", app_file.read_text())
        self.assertEqual(unrelated.read_text(), "# Project Info\nDo not change me\n")

    def test_scenario_4_failure_recovery_rejected_write(self):
        """Scenario 4: User rejects file overwrite; agent handles gracefully without corrupting disk."""
        target = Path(self.temp_proj) / "important.txt"
        target.write_text("ORIGINAL CONTENT", encoding="utf-8")

        history = []
        call_seq = iter([
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "c1",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "important.txt", "content": "DESTRUCTIVE"})
                    }
                }]
            },
            {
                "role": "assistant",
                "content": "Penulisan dibatalkan oleh pengguna. File penting tetap aman.",
            }
        ])

        # Simulate user typing 'n' to reject overwrite
        builtins.input = lambda *a: "n"

        with patch("arcdvncli.agent.chat", side_effect=call_seq):
            res = agent.run_agent("Overwrite important.txt", history)

        self.assertEqual(target.read_text(), "ORIGINAL CONTENT")
        self.assertIn("important.txt", res)


# ============================================================================
# E. SECURITY AND FILESYSTEM TESTS
# ============================================================================
class TestSecurityAndFilesystem(BaseIsolatedTest):

    def test_sensitive_files_write_blocked(self):
        """Security: write_file raises PermissionError when targeting .env or private keys."""
        with self.assertRaises(PermissionError):
            tools.write_file(".env", "MALICIOUS=true")
        with self.assertRaises(PermissionError):
            tools.write_file("id_rsa", "PRIVATE KEY")
        with self.assertRaises(PermissionError):
            tools.write_file(".git/config", "INJECTED")

    def test_sensitive_files_read_and_edit_blocked(self):
        """Security: read_file and edit_file block sensitive files."""
        (Path(self.temp_proj) / ".env").write_text("TOKEN=123", encoding="utf-8")
        with self.assertRaises(PermissionError):
            tools.read_file(".env")
        with self.assertRaises(PermissionError):
            tools.edit_file(".env", "123", "456")

    def test_path_traversal_blocked(self):
        """Security: Paths attempting traversal outside project are rejected."""
        with self.assertRaises(ValueError):
            tools.safe_path("../../outside.txt")
        with self.assertRaises(ValueError):
            tools.safe_path("/etc/passwd")

    def test_null_byte_path_rejected(self):
        """Security: Paths containing null bytes are rejected."""
        with self.assertRaises(ValueError):
            tools.safe_path("foo\x00bar.txt")


if __name__ == "__main__":
    unittest.main()
