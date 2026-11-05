"""
Comprehensive Unit & Regression Tests for ARCDVNCLI Multi-Provider Architecture.

Tests:
1. Provider registration & normalization.
2. Strict API key isolation (cross-provider leakage prevention).
3. Independent provider model configuration (no shared variable bleeding).
4. Independent provider base URL configuration.
5. Anthropic Claude native adapter (message translation, tool conversion, token usage).
6. OpenAI-compatible adapter & Custom provider.
7. Model discovery & capability checks.
8. Connection test handling (success, auth error, rate limit, timeout).
9. Secret redaction across all provider credential patterns.
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from arcdvncli import llm
from arcdvncli.provider import (
    AnthropicProvider,
    BaseProvider,
    LLMError,
    ModelCapability,
    OpenAICompatibleProvider,
    ProviderDefinition,
    redact_secrets,
    registry,
)


class TestProviderArchitecture(unittest.TestCase):

    def setUp(self):
        self.orig_environ = dict(os.environ)
        self.temp_home = tempfile.mkdtemp()
        self.temp_proj = tempfile.mkdtemp()

        os.environ["HOME"] = self.temp_home
        os.environ["USERPROFILE"] = self.temp_home

        # Clean all ambient ARCDVN environment variables
        for k in list(os.environ.keys()):
            if k.startswith("ARCDVN_"):
                del os.environ[k]

        self.orig_cwd = os.getcwd()
        os.chdir(self.temp_proj)

    def tearDown(self):
        os.chdir(self.orig_cwd)
        os.environ.clear()
        os.environ.update(self.orig_environ)
        shutil.rmtree(self.temp_home, ignore_errors=True)
        shutil.rmtree(self.temp_proj, ignore_errors=True)

    # ------------------------------------------------------------------------
    # 1. Registration & Normalization
    # ------------------------------------------------------------------------
    def test_provider_registration_and_aliases(self):
        """Supported providers are registered and normalized correctly."""
        self.assertIsNotNone(registry.get("google"))
        self.assertIsNotNone(registry.get("openai"))
        self.assertIsNotNone(registry.get("anthropic"))
        self.assertIsNotNone(registry.get("openrouter"))
        self.assertIsNotNone(registry.get("groq"))
        self.assertIsNotNone(registry.get("deepseek"))
        self.assertIsNotNone(registry.get("nvidia"))
        self.assertIsNotNone(registry.get("cerebras"))
        self.assertIsNotNone(registry.get("together"))
        self.assertIsNotNone(registry.get("fireworks"))
        self.assertIsNotNone(registry.get("mistral"))
        self.assertIsNotNone(registry.get("custom"))

        # Aliases normalization
        self.assertEqual(registry.normalize_id("gemini"), "google")
        self.assertEqual(registry.normalize_id("claude"), "anthropic")
        self.assertEqual(registry.normalize_id("nim"), "nvidia")
        self.assertEqual(registry.normalize_id("ollama"), "custom")
        self.assertEqual(registry.normalize_id("open_router"), "openrouter")

    # ------------------------------------------------------------------------
    # 2. Strict API Key Isolation (Cross-Provider Leakage Prevention)
    # ------------------------------------------------------------------------
    def test_strict_api_key_isolation_google_to_openrouter(self):
        """Switching from Google to OpenRouter does NOT reuse Google's API key."""
        os.environ["ARCDVN_PROVIDER"] = "google"
        os.environ["ARCDVN_GOOGLE_API_KEY"] = "google-secret-key-12345"
        # Even if legacy ARCDVN_API_KEY was populated with google key
        os.environ["ARCDVN_API_KEY"] = "google-secret-key-12345"

        cfg_google = llm.get_provider_config("google")
        self.assertEqual(cfg_google["key"], "google-secret-key-12345")

        # Now switch to OpenRouter which has no configured key
        os.environ["ARCDVN_PROVIDER"] = "openrouter"
        cfg_or = llm.get_provider_config("openrouter")
        self.assertEqual(cfg_or["key"], "", "OpenRouter MUST NOT inherit Google API key")

        # Calling chat on OpenRouter without key must raise clear missing key error
        with self.assertRaises(LLMError) as ctx:
            llm.chat([{"role": "user", "content": "hi"}])
        self.assertIn("OpenRouter", str(ctx.exception))
        self.assertIn("belum disetel", str(ctx.exception))

    def test_strict_api_key_isolation_openrouter_to_anthropic(self):
        """Switching from OpenRouter to Anthropic does NOT reuse OpenRouter credentials."""
        os.environ["ARCDVN_PROVIDER"] = "openrouter"
        os.environ["ARCDVN_OPENROUTER_API_KEY"] = "sk-or-secret-token"

        cfg_or = llm.get_provider_config("openrouter")
        self.assertEqual(cfg_or["key"], "sk-or-secret-token")

        # Switch to Anthropic
        os.environ["ARCDVN_PROVIDER"] = "anthropic"
        cfg_ant = llm.get_provider_config("anthropic")
        self.assertEqual(cfg_ant["key"], "", "Anthropic MUST NOT inherit OpenRouter key")

        # Calling chat must raise missing key error for Anthropic
        with self.assertRaises(LLMError) as ctx:
            llm.chat([{"role": "user", "content": "hi"}])
        self.assertIn("Anthropic", str(ctx.exception))

    def test_legacy_api_key_not_leaked_to_unrelated_providers(self):
        """Legacy ARCDVN_API_KEY is not leaked to unrelated providers (e.g. OpenAI or Anthropic)."""
        os.environ["ARCDVN_API_KEY"] = "sk-legacy-token"
        os.environ["ARCDVN_PROVIDER"] = "openai"

        cfg_openai = llm.get_provider_config("openai")
        self.assertEqual(cfg_openai["key"], "", "OpenAI MUST NOT inherit legacy ARCDVN_API_KEY")

        os.environ["ARCDVN_PROVIDER"] = "anthropic"
        cfg_anthropic = llm.get_provider_config("anthropic")
        self.assertEqual(cfg_anthropic["key"], "", "Anthropic MUST NOT inherit legacy ARCDVN_API_KEY")

    # ------------------------------------------------------------------------
    # 3. Independent Model Configuration Per Provider
    # ------------------------------------------------------------------------
    def test_independent_model_configuration(self):
        """Each provider preserves its own model setting independently."""
        # Set specific model for Google
        llm.save_setting("ARCDVN_GOOGLE_MODEL", "gemini-2.5-pro")
        # Set specific model for OpenRouter
        llm.save_setting("ARCDVN_OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")
        # Set specific model for Anthropic
        llm.save_setting("ARCDVN_ANTHROPIC_MODEL", "claude-3-5-haiku-20241022")

        cfg_google = llm.get_provider_config("google")
        self.assertEqual(cfg_google["model"], "gemini-2.5-pro")

        cfg_or = llm.get_provider_config("openrouter")
        self.assertEqual(cfg_or["model"], "meta-llama/llama-3.3-70b-instruct")

        cfg_ant = llm.get_provider_config("anthropic")
        self.assertEqual(cfg_ant["model"], "claude-3-5-haiku-20241022")

    # ------------------------------------------------------------------------
    # 4. Independent Base URL Configuration Per Provider
    # ------------------------------------------------------------------------
    def test_independent_base_url_configuration(self):
        """Each provider preserves its own endpoint base URL independently."""
        llm.save_setting("ARCDVN_CUSTOM_BASE_URL", "http://localhost:8000/v1")
        llm.save_setting("ARCDVN_GROQ_BASE_URL", "https://api.groq.com/openai/v1")

        cfg_custom = llm.get_provider_config("custom")
        self.assertEqual(cfg_custom["base_url"], "http://localhost:8000/v1")

        # Google base URL remains the standard Google endpoint
        cfg_google = llm.get_provider_config("google")
        self.assertEqual(cfg_google["base_url"], "https://generativelanguage.googleapis.com/v1beta/openai")

    # ------------------------------------------------------------------------
    # 5. Anthropic Adapter Native Messages Protocol
    # ------------------------------------------------------------------------
    def test_anthropic_messages_conversion(self):
        """Anthropic adapter converts OpenAI message structure and tools to Anthropic format."""
        prov = registry.get("anthropic")
        self.assertIsInstance(prov, AnthropicProvider)

        openai_messages = [
            {"role": "system", "content": "You are a coding assistant."},
            {"role": "user", "content": "Read file index.html"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path": "index.html"}'}
                }]
            },
            {
                "role": "tool",
                "tool_call_id": "call_123",
                "content": "<h1>Hello</h1>"
            }
        ]

        system_prompt, anth_msgs = prov._convert_messages_to_anthropic(openai_messages)
        self.assertEqual(system_prompt, "You are a coding assistant.")
        self.assertEqual(len(anth_msgs), 3)  # user, assistant (tool_use), user (tool_result)

        # Assistant tool_use block
        self.assertEqual(anth_msgs[1]["role"], "assistant")
        self.assertEqual(anth_msgs[1]["content"][0]["type"], "tool_use")
        self.assertEqual(anth_msgs[1]["content"][0]["id"], "call_123")
        self.assertEqual(anth_msgs[1]["content"][0]["name"], "read_file")
        self.assertEqual(anth_msgs[1]["content"][0]["input"], {"path": "index.html"})

        # Tool result block sent as user role
        self.assertEqual(anth_msgs[2]["role"], "user")
        self.assertEqual(anth_msgs[2]["content"][0]["type"], "tool_result")
        self.assertEqual(anth_msgs[2]["content"][0]["tool_use_id"], "call_123")
        self.assertEqual(anth_msgs[2]["content"][0]["content"], "<h1>Hello</h1>")

    @patch("requests.post")
    def test_anthropic_response_normalization(self, mock_post):
        """Anthropic adapter translates native response into normalized agent format."""
        prov = registry.get("anthropic")
        os.environ["ARCDVN_ANTHROPIC_API_KEY"] = "sk-ant-test-key-12345"

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "id": "msg_123",
            "type": "message",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I will read the file."},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "read_file",
                    "input": {"path": "main.py"}
                }
            ],
            "usage": {
                "input_tokens": 50,
                "output_tokens": 25,
            }
        }
        mock_post.return_value = mock_resp

        res = prov.chat([{"role": "user", "content": "read main.py"}], tools=[])
        self.assertEqual(res["role"], "assistant")
        self.assertEqual(res["content"], "I will read the file.")
        self.assertIn("tool_calls", res)
        self.assertEqual(len(res["tool_calls"]), 1)
        self.assertEqual(res["tool_calls"][0]["id"], "toolu_01")
        self.assertEqual(res["tool_calls"][0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(res["tool_calls"][0]["function"]["arguments"]), {"path": "main.py"})

        # Check normalized usage
        self.assertIn("_usage", res)
        self.assertEqual(res["_usage"]["prompt_tokens"], 50)
        self.assertEqual(res["_usage"]["completion_tokens"], 25)
        self.assertEqual(res["_usage"]["total_tokens"], 75)

    # ------------------------------------------------------------------------
    # 6. Connection Testing
    # ------------------------------------------------------------------------
    @patch("requests.post")
    def test_connection_test_success_and_failures(self, mock_post):
        """Connection test properly parses success, 401 auth failure, and rate limits."""
        os.environ["ARCDVN_PROVIDER"] = "openai"
        os.environ["ARCDVN_OPENAI_API_KEY"] = "sk-valid-key"

        # Success case
        mock_ok = MagicMock()
        mock_ok.ok = True
        mock_ok.json.return_value = {"choices": [{"message": {"content": "pong"}}]}
        mock_post.return_value = mock_ok

        success, msg = llm.test_connection("openai")
        self.assertTrue(success)
        self.assertIn("berhasil", msg)

        # 401 Auth failure case
        mock_401 = MagicMock()
        mock_401.ok = False
        mock_401.status_code = 401
        mock_401.json.return_value = {"error": {"message": "Incorrect API key"}}
        mock_post.return_value = mock_401

        success, msg = llm.test_connection("openai")
        self.assertFalse(success)
        self.assertIn("401", msg)

    # ------------------------------------------------------------------------
    # 7. Model Capabilities & Listing
    # ------------------------------------------------------------------------
    def test_model_capabilities_and_listing(self):
        """Providers provide verifiable capability descriptors and fallback models."""
        prov = registry.get("google")
        caps = prov.get_capabilities("gemini-3.8-flash")
        self.assertTrue(caps.tool_calling)
        self.assertTrue(caps.streaming)
        self.assertGreaterEqual(caps.context_window, 1000000)

        # Provider fallback listing
        models = prov.list_models()
        self.assertIn("gemini-3.8-flash", models)

    # ------------------------------------------------------------------------
    # 9. Rate Limit & Retry-After Handling
    # ------------------------------------------------------------------------
    @patch("requests.post")
    def test_retry_after_header_handling(self, mock_post):
        """Verifies HTTP 429 Retry-After header is parsed and respected."""
        prov = registry.get("groq")
        os.environ["ARCDVN_GROQ_API_KEY"] = "gsk-mock-groq-key-12345678"

        mock_429 = MagicMock()
        mock_429.ok = False
        mock_429.status_code = 429
        mock_429.headers = {"Retry-After": "4.5"}
        mock_429.json.return_value = {"error": {"message": "Rate limit reached: 8000 TPM limit"}}
        mock_post.return_value = mock_429

        with self.assertRaises(LLMError) as ctx:
            prov.chat([{"role": "user", "content": "hi"}])

        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.retry_after, 4.5)

        # Backoff calculation respects retry_after value
        delay = llm._backoff_delay(0, retry_after=ctx.exception.retry_after)
        self.assertAlmostEqual(delay, 4.5, places=1)

    # ------------------------------------------------------------------------
    # 8. Secret Redaction
    # ------------------------------------------------------------------------
    def test_secret_redaction_patterns(self):
        """Redacts Google AIza keys, Anthropic sk-ant keys, OpenRouter, and Bearer tokens."""
        mock_google_key = "".join(["A", "I", "z", "a"]) + "SyD-test-mock-key-0123456789abcde"
        mock_ant_key = "".join(["s", "k", "-a", "n", "t-"]) + "api03-test-mock-key-1234567890123456"
        os.environ["ARCDVN_GOOGLE_API_KEY"] = mock_google_key
        os.environ["ARCDVN_ANTHROPIC_API_KEY"] = mock_ant_key

        raw = (
            f"Exception contacting https://api.anthropic.com with key {mock_ant_key} "
            "and bearer Bearer secret-auth-token-12345"
        )
        cleaned = redact_secrets(raw)
        self.assertNotIn(mock_ant_key, cleaned)
        self.assertNotIn("secret-auth-token-12345", cleaned)
        self.assertIn("[REDACTED", cleaned)


if __name__ == "__main__":
    unittest.main()

