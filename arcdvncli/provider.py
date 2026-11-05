"""
Multi-Provider Architecture for ARCDVNCLI.
Defines normalized provider interfaces, adapters, registry, and model management.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import requests


@dataclass
class ModelCapability:
    tool_calling: bool = True
    streaming: bool = True
    vision: bool = False
    context_window: int = 128000
    max_output_tokens: int = 8192
    description: str = ""


@dataclass
class ProviderDefinition:
    id: str
    name: str
    default_base_url: str
    default_model: str
    env_key: str
    models_hint: str
    adapter_type: str = "openai_compatible"
    models: List[str] = field(default_factory=list)
    capabilities: Dict[str, ModelCapability] = field(default_factory=dict)
    standard_env_keys: List[str] = field(default_factory=list)


class LLMError(RuntimeError):
    """LLM request failed with a categorized reason."""
    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after = retry_after


# ---------------------------------------------------------------------------
# Redaction helpers
# ---------------------------------------------------------------------------
def redact_secrets(text: Any) -> str:
    """Redact known API keys and common API token patterns from strings."""
    if not text:
        return ""
    result = str(text)

    # Redact environment-stored keys
    for var, val in os.environ.items():
        if (var.startswith("ARCDVN_") or var.endswith("_API_KEY")) and "KEY" in var:
            if val and len(val) >= 6:
                result = result.replace(val, "[REDACTED_API_KEY]")

    # Redact common credential patterns
    result = re.sub(r'AIza[0-9A-Za-z-_]{35}', '[REDACTED_GOOGLE_KEY]', result)
    result = re.sub(r'sk-or-v1-[0-9a-fA-F]{64}', '[REDACTED_OPENROUTER_KEY]', result)
    result = re.sub(r'sk-ant-[0-9a-zA-Z\-_]{30,}', '[REDACTED_ANTHROPIC_KEY]', result)
    result = re.sub(r'sk-[a-zA-Z0-9]{20,}', '[REDACTED_API_KEY]', result)
    result = re.sub(r'nvapi-[0-9a-zA-Z_\-]{30,}', '[REDACTED_NVIDIA_KEY]', result)
    result = re.sub(r'Bearer\s+[A-Za-z0-9_\-\.]{8,}', 'Bearer [REDACTED_TOKEN]', result)
    return result


# ---------------------------------------------------------------------------
# Base Provider Adapter
# ---------------------------------------------------------------------------
class BaseProvider:
    def __init__(self, definition: ProviderDefinition):
        self.definition = definition

    @property
    def id(self) -> str:
        return self.definition.id

    @property
    def name(self) -> str:
        return self.definition.name

    def get_api_key(self) -> str:
        """
        Retrieve provider-specific API key strictly.
        Never fall back to credentials from other providers.
        """
        # Primary provider-specific key: ARCDVN_<PROVIDER>_API_KEY
        key = os.getenv(self.definition.env_key, "").strip()
        if key:
            return key

        # Standard industry env vars (e.g. GEMINI_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY)
        for std_key in self.definition.standard_env_keys:
            val = os.getenv(std_key, "").strip()
            if val:
                return val

        # Controlled legacy fallback:
        # ARCDVN_API_KEY is ONLY permitted if:
        # - This provider is a legacy provider ('google' or 'openrouter')
        # - NO other provider has a specific key configured (preventing leakage after provider switching)
        legacy_key = os.getenv("ARCDVN_API_KEY", "").strip()
        if legacy_key and self.id in ("google", "openrouter"):
            has_other_key = any(
                os.getenv(k, "").strip()
                for k in [
                    "ARCDVN_GOOGLE_API_KEY",
                    "ARCDVN_OPENROUTER_API_KEY",
                    "ARCDVN_OPENAI_API_KEY",
                    "ARCDVN_ANTHROPIC_API_KEY",
                    "ARCDVN_GROQ_API_KEY",
                    "ARCDVN_DEEPSEEK_API_KEY",
                ]
                if k != self.definition.env_key
            )
            if not has_other_key:
                active_id = self._get_active_provider_id()
                if active_id == self.id:
                    return legacy_key

        return ""

    def _get_active_provider_id(self) -> str:
        env_prov = os.getenv("ARCDVN_PROVIDER")
        if env_prov:
            return registry.normalize_id(env_prov)
        base = (os.getenv("ARCDVN_BASE_URL") or "").lower()
        if "googleapis.com" in base or "generativelanguage" in base:
            return "google"
        if "openrouter.ai" in base:
            return "openrouter"
        if os.getenv("ARCDVN_GOOGLE_API_KEY"):
            return "google"
        if os.getenv("ARCDVN_OPENROUTER_API_KEY"):
            return "openrouter"
        if os.getenv("ARCDVN_API_KEY"):
            model = os.getenv("ARCDVN_MODEL", "")
            if "gemini-3" in model or ("gemini-2" in model and "/" not in model):
                return "google"
            return "openrouter"
        return "google"

    def get_base_url(self) -> str:
        """Retrieve provider base URL, preferring provider-specific override."""
        prov_var = f"ARCDVN_{self.id.upper()}_BASE_URL"
        custom_base = os.getenv(prov_var, "").strip()
        if custom_base:
            return custom_base.rstrip("/")
        # Global override only if explicitly set and active
        global_base = os.getenv("ARCDVN_BASE_URL", "").strip()
        if global_base and self.id in ("custom",):
            return global_base.rstrip("/")
        return self.definition.default_base_url.rstrip("/")

    def get_model(self) -> str:
        """Retrieve active model for this provider."""
        prov_var = f"ARCDVN_{self.id.upper()}_MODEL"
        custom_model = os.getenv(prov_var, "").strip()
        if custom_model:
            return custom_model
        # Global ARCDVN_MODEL fallback if set
        global_model = os.getenv("ARCDVN_MODEL", "").strip()
        if global_model:
            # If global model belongs to this provider style
            return global_model
        return self.definition.default_model

    def get_capabilities(self, model: Optional[str] = None) -> ModelCapability:
        model_name = model or self.get_model()
        if model_name in self.definition.capabilities:
            return self.definition.capabilities[model_name]
        # Sensible default
        return ModelCapability(tool_calling=True, streaming=True)

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        raise NotImplementedError

    def test_connection(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> Tuple[bool, str]:
        raise NotImplementedError

    def list_models(self) -> List[str]:
        return list(self.definition.models)


# ---------------------------------------------------------------------------
# OpenAI-Compatible Provider Adapter
# ---------------------------------------------------------------------------
class OpenAICompatibleProvider(BaseProvider):
    """
    Adapter for OpenAI and OpenAI-compatible endpoints:
    Google Gemini OpenAI-compat, OpenRouter, OpenAI, Groq, DeepSeek,
    NVIDIA NIM, Cerebras, Together AI, Fireworks AI, Mistral, and Custom endpoints.
    """

    def _build_headers(self, key: str) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        if self.id == "openrouter":
            headers["HTTP-Referer"] = "https://github.com/Magelanicc/arcdvncli"
            headers["X-Title"] = "ARCDVNCLI"
        return headers

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        key = self.get_api_key()
        if not key:
            raise LLMError(
                f"API Key untuk provider '{self.name}' belum disetel. "
                f"Gunakan perintah /settings atau setel {self.definition.env_key} di ~/.arcdvncli_env."
            )

        base = self.get_base_url()
        model = self.get_model()
        endpoint = f"{base}/chat/completions"
        headers = self._build_headers(key)

        clean_messages = []
        for m in messages:
            if isinstance(m, dict):
                clean_messages.append({k: v for k, v in m.items() if not k.startswith("_")})
            else:
                clean_messages.append(m)

        payload: Dict[str, Any] = {
            "model": model,
            "messages": clean_messages,
        }

        if tools:
            payload["tools"] = [{"type": "function", "function": t} for t in tools]
            payload["tool_choice"] = "auto"

        try:
            r = requests.post(endpoint, headers=headers, json=payload, timeout=120)
        except requests.exceptions.Timeout:
            raise LLMError("Request ke LLM timeout setelah 120 detik. Server mungkin sibuk.", retryable=True)
        except requests.exceptions.ConnectionError as e:
            raise LLMError(
                f"Koneksi ke LLM ({self.name}) gagal: {redact_secrets(str(e))}. "
                "Periksa koneksi internet atau base URL.",
                retryable=True,
            )
        except requests.exceptions.RequestException as e:
            raise LLMError(f"Request error: {redact_secrets(str(e))}", retryable=False)

        if not r.ok:
            retryable, msg = self._classify_http_error(r)
            retry_after_val = None
            raw_ra = r.headers.get("Retry-After")
            if raw_ra:
                try:
                    retry_after_val = float(raw_ra)
                except (ValueError, TypeError):
                    pass
            raise LLMError(
                msg,
                retryable=retryable,
                status_code=r.status_code,
                retry_after=retry_after_val,
            )

        try:
            data = r.json()
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMError(
                f"Respons LLM bukan JSON valid: {e}. Body: {redact_secrets(r.text[:300])}",
                retryable=True,
            )

        if "choices" not in data or not data["choices"]:
            err_msg = ""
            if isinstance(data.get("error"), dict):
                err_msg = data["error"].get("message", "")
            elif isinstance(data.get("error"), str):
                err_msg = data["error"]
            is_overload = any(
                kw in str(err_msg).lower()
                for kw in ("overloaded", "capacity", "unavailable", "busy", "resource exhausted")
            )
            raise LLMError(
                f"API tidak mengembalikan 'choices'. Error: {redact_secrets(str(err_msg)) or '(tidak ada)'}",
                retryable=is_overload,
            )

        choice = data["choices"][0]
        message = choice.get("message", {})

        # Normalize usage
        usage = data.get("usage")
        if usage and isinstance(usage, dict) and isinstance(message, dict):
            message["_usage"] = {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }

        return message

    def test_connection(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> Tuple[bool, str]:
        key = api_key or self.get_api_key()
        if not key:
            return False, f"API key untuk {self.name} belum diisi."

        base = (base_url or self.get_base_url()).rstrip("/")
        model_name = model or self.get_model()
        endpoint = f"{base}/chat/completions"
        headers = self._build_headers(key)

        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }

        try:
            t0 = time.time()
            r = requests.post(endpoint, headers=headers, json=payload, timeout=20)
            latency = time.time() - t0

            if not r.ok:
                _, msg = self._classify_http_error(r)
                return False, msg

            data = r.json()
            if "choices" not in data or not data["choices"]:
                err_msg = data.get("error", {}).get("message", "Tidak ada respon 'choices'") if isinstance(data.get("error"), dict) else "Respons kosong"
                return False, redact_secrets(str(err_msg))

            return True, f"Koneksi ke {self.name} ({model_name}) berhasil! Latency: {latency:.2f}s"
        except requests.exceptions.Timeout:
            return False, "Koneksi timeout setelah 20 detik. Periksa koneksi internet."
        except requests.exceptions.ConnectionError as e:
            return False, f"Gagal terhubung ke host: {redact_secrets(str(e))}"
        except Exception as e:
            return False, f"Error: {redact_secrets(str(e))}"

    def list_models(self) -> List[str]:
        key = self.get_api_key()
        if not key:
            return list(self.definition.models)

        base = self.get_base_url()
        endpoint = f"{base}/models"
        headers = self._build_headers(key)

        try:
            r = requests.get(endpoint, headers=headers, timeout=5)
            if r.ok:
                data = r.json()
                items = data.get("data") or data.get("models") or []
                discovered = []
                for item in items:
                    if isinstance(item, dict) and "id" in item:
                        discovered.append(item["id"])
                    elif isinstance(item, str):
                        discovered.append(item)
                if discovered:
                    return sorted(discovered)
        except Exception:
            pass

        return list(self.definition.models)

    def _classify_http_error(self, response: requests.Response) -> Tuple[bool, str]:
        code = response.status_code
        try:
            body = response.json()
            err_msg = ""
            if isinstance(body.get("error"), dict):
                err_msg = body["error"].get("message", "")
            elif isinstance(body.get("error"), str):
                err_msg = body["error"]
            elif isinstance(body.get("message"), str):
                err_msg = body["message"]
        except Exception:
            err_msg = response.text[:500]

        err_msg = redact_secrets(err_msg)

        if code in {400, 401, 403, 404}:
            hints = {
                400: "Permintaan ditolak oleh provider (Bad Request). Periksa parameter model atau format payload.",
                401: f"API key untuk {self.name} tidak valid atau kedaluwarsa. Periksa pengaturan di /settings.",
                403: f"Akses ditolak atau kuota habis pada {self.name}. Periksa saldo atau izin model.",
                404: f"Model atau endpoint {self.name} tidak ditemukan. Periksa nama model atau URL endpoint.",
            }
            detail = hints.get(code, err_msg)
            return False, f"LLM error {code}: {detail}"

        if code in {429, 500, 502, 503, 504}:
            return True, f"LLM error {code} (sementara): {err_msg}"

        return False, f"LLM error {code}: {err_msg}"


# ---------------------------------------------------------------------------
# Anthropic Native Adapter
# ---------------------------------------------------------------------------
class AnthropicProvider(BaseProvider):
    """
    Dedicated native adapter for Anthropic Claude Messages API (/v1/messages).
    Normalizes tool schemas, input/output messages, and token usage to match
    ARCDVNCLI's agent core expectations.
    """

    def _build_headers(self, key: str) -> Dict[str, str]:
        return {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def _convert_messages_to_anthropic(
        self, messages: List[Dict[str, Any]]
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        Convert OpenAI-style message sequence into Anthropic system prompt + messages list.
        Transforms OpenAI tool calls and tool responses into Anthropic's tool_use and tool_result blocks.
        """
        system_prompt = ""
        anthropic_msgs: List[Dict[str, Any]] = []

        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""

            if role == "system":
                if system_prompt:
                    system_prompt += "\n\n" + str(content)
                else:
                    system_prompt = str(content)
            elif role == "user":
                anthropic_msgs.append({"role": "user", "content": str(content)})
            elif role == "assistant":
                tool_calls = m.get("tool_calls")
                if tool_calls:
                    blocks: List[Dict[str, Any]] = []
                    if content:
                        blocks.append({"type": "text", "text": str(content)})
                    for tc in tool_calls:
                        call_id = tc.get("id", "call_1")
                        func = tc.get("function", {})
                        func_name = func.get("name", "")
                        raw_args = func.get("arguments", "{}")
                        try:
                            args_dict = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                        except Exception:
                            args_dict = {}
                        blocks.append({
                            "type": "tool_use",
                            "id": call_id,
                            "name": func_name,
                            "input": args_dict,
                        })
                    anthropic_msgs.append({"role": "assistant", "content": blocks})
                else:
                    anthropic_msgs.append({"role": "assistant", "content": str(content)})
            elif role == "tool":
                tool_call_id = m.get("tool_call_id", "")
                result_content = str(content)
                # Anthropic tool_result must be sent inside role='user'
                tool_block = {
                    "type": "tool_result",
                    "tool_use_id": tool_call_id,
                    "content": result_content,
                }
                # If previous message is already user, append to its content block list
                if anthropic_msgs and anthropic_msgs[-1]["role"] == "user":
                    prev_content = anthropic_msgs[-1]["content"]
                    if isinstance(prev_content, list):
                        prev_content.append(tool_block)
                    else:
                        anthropic_msgs[-1]["content"] = [
                            {"type": "text", "text": str(prev_content)},
                            tool_block,
                        ]
                else:
                    anthropic_msgs.append({
                        "role": "user",
                        "content": [tool_block],
                    })

        return system_prompt, anthropic_msgs

    def _convert_tools_to_anthropic(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert OpenAI tool schema to Anthropic tool schema."""
        anthropic_tools = []
        for t in tools:
            # t is a function dict
            name = t.get("name", "")
            description = t.get("description", "")
            parameters = t.get("parameters", {"type": "object", "properties": {}})
            anthropic_tools.append({
                "name": name,
                "description": description,
                "input_schema": parameters,
            })
        return anthropic_tools

    def chat(self, messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        key = self.get_api_key()
        if not key:
            raise LLMError(
                f"API Key untuk provider '{self.name}' belum disetel. "
                f"Gunakan perintah /settings atau setel {self.definition.env_key} di ~/.arcdvncli_env."
            )

        base = self.get_base_url()
        model = self.get_model()
        endpoint = f"{base}/messages"
        headers = self._build_headers(key)

        system_prompt, anth_messages = self._convert_messages_to_anthropic(messages)

        payload: Dict[str, Any] = {
            "model": model,
            "messages": anth_messages,
            "max_tokens": 4096,
        }
        if system_prompt:
            payload["system"] = system_prompt

        if tools:
            payload["tools"] = self._convert_tools_to_anthropic(tools)

        try:
            r = requests.post(endpoint, headers=headers, json=payload, timeout=120)
        except requests.exceptions.Timeout:
            raise LLMError("Request ke Anthropic timeout setelah 120 detik.", retryable=True)
        except requests.exceptions.ConnectionError as e:
            raise LLMError(f"Koneksi ke Anthropic gagal: {redact_secrets(str(e))}", retryable=True)
        except requests.exceptions.RequestException as e:
            raise LLMError(f"Request error: {redact_secrets(str(e))}", retryable=False)

        if not r.ok:
            retryable, msg = self._classify_http_error(r)
            retry_after_val = None
            raw_ra = r.headers.get("Retry-After")
            if raw_ra:
                try:
                    retry_after_val = float(raw_ra)
                except (ValueError, TypeError):
                    pass
            raise LLMError(
                msg,
                retryable=retryable,
                status_code=r.status_code,
                retry_after=retry_after_val,
            )

        try:
            data = r.json()
        except (ValueError, json.JSONDecodeError) as e:
            raise LLMError(f"Respons Anthropic bukan JSON valid: {e}", retryable=True)

        content_blocks = data.get("content") or []
        text_parts = []
        tool_calls = []

        for block in content_blocks:
            b_type = block.get("type")
            if b_type == "text":
                text_parts.append(block.get("text", ""))
            elif b_type == "tool_use":
                tool_calls.append({
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(block.get("input") or {}),
                    },
                })

        normalized_message: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(text_parts) if text_parts else None,
        }
        if tool_calls:
            normalized_message["tool_calls"] = tool_calls

        # Normalize usage
        usage = data.get("usage")
        if usage and isinstance(usage, dict):
            p_tok = usage.get("input_tokens", 0)
            c_tok = usage.get("output_tokens", 0)
            normalized_message["_usage"] = {
                "prompt_tokens": p_tok,
                "completion_tokens": c_tok,
                "total_tokens": p_tok + c_tok,
            }

        return normalized_message

    def test_connection(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> Tuple[bool, str]:
        key = api_key or self.get_api_key()
        if not key:
            return False, f"API key untuk {self.name} belum diisi."

        base = (base_url or self.get_base_url()).rstrip("/")
        model_name = model or self.get_model()
        endpoint = f"{base}/messages"
        headers = self._build_headers(key)

        payload = {
            "model": model_name,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }

        try:
            t0 = time.time()
            r = requests.post(endpoint, headers=headers, json=payload, timeout=20)
            latency = time.time() - t0

            if not r.ok:
                _, msg = self._classify_http_error(r)
                return False, msg

            return True, f"Koneksi ke Anthropic ({model_name}) berhasil! Latency: {latency:.2f}s"
        except requests.exceptions.Timeout:
            return False, "Koneksi timeout setelah 20 detik."
        except requests.exceptions.ConnectionError as e:
            return False, f"Gagal terhubung ke host: {redact_secrets(str(e))}"
        except Exception as e:
            return False, f"Error: {redact_secrets(str(e))}"

    def _classify_http_error(self, response: requests.Response) -> Tuple[bool, str]:
        code = response.status_code
        try:
            body = response.json()
            err_msg = ""
            if isinstance(body.get("error"), dict):
                err_msg = body["error"].get("message", "")
            elif isinstance(body.get("error"), str):
                err_msg = body["error"]
        except Exception:
            err_msg = response.text[:500]

        err_msg = redact_secrets(err_msg)

        if code in {400, 401, 403, 404}:
            hints = {
                401: "API key Anthropic tidak valid. Periksa x-api-key di /settings.",
                403: "Akses ditolak atau kuota habis pada akun Anthropic Anda.",
                404: "Model Anthropic tidak ditemukan. Periksa nama model.",
            }
            detail = hints.get(code, err_msg)
            return False, f"Anthropic error {code}: {detail}"

        if code in {429, 500, 502, 503, 504, 529}:
            return True, f"Anthropic error {code} (sementara): {err_msg}"

        return False, f"Anthropic error {code}: {err_msg}"


# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------
class ProviderRegistry:
    def __init__(self):
        self._providers: Dict[str, BaseProvider] = {}
        self._init_defaults()

    def _init_defaults(self):
        # 1. Google Gemini
        self.register(ProviderDefinition(
            id="google",
            name="Google Gemini",
            default_base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            default_model="gemini-3.8-flash",
            env_key="ARCDVN_GOOGLE_API_KEY",
            standard_env_keys=["GEMINI_API_KEY", "GOOGLE_API_KEY"],
            models_hint="gemini-3.8-flash, gemini-2.5-flash, gemini-2.5-pro",
            models=["gemini-3.8-flash", "gemini-2.5-flash", "gemini-2.5-pro", "gemini-1.5-pro"],
            capabilities={
                "gemini-3.8-flash": ModelCapability(tool_calling=True, streaming=True, context_window=1000000),
                "gemini-2.5-flash": ModelCapability(tool_calling=True, streaming=True, context_window=1000000),
                "gemini-2.5-pro": ModelCapability(tool_calling=True, streaming=True, context_window=2000000),
            },
        ))

        # 2. OpenRouter
        self.register(ProviderDefinition(
            id="openrouter",
            name="OpenRouter",
            default_base_url="https://openrouter.ai/api/v1",
            default_model="google/gemini-2.5-flash",
            env_key="ARCDVN_OPENROUTER_API_KEY",
            standard_env_keys=["OPENROUTER_API_KEY"],
            models_hint="google/gemini-2.5-flash, deepseek/deepseek-chat, anthropic/claude-3.5-sonnet",
            models=[
                "google/gemini-2.5-flash",
                "deepseek/deepseek-chat",
                "anthropic/claude-3.5-sonnet",
                "meta-llama/llama-3.3-70b-instruct",
            ],
        ))

        # 3. OpenAI
        self.register(ProviderDefinition(
            id="openai",
            name="OpenAI",
            default_base_url="https://api.openai.com/v1",
            default_model="gpt-4o",
            env_key="ARCDVN_OPENAI_API_KEY",
            standard_env_keys=["OPENAI_API_KEY"],
            models_hint="gpt-4o, gpt-4o-mini, o1, o3-mini",
            models=["gpt-4o", "gpt-4o-mini", "o1", "o3-mini", "gpt-4-turbo"],
            capabilities={
                "gpt-4o": ModelCapability(tool_calling=True, streaming=True, context_window=128000),
                "gpt-4o-mini": ModelCapability(tool_calling=True, streaming=True, context_window=128000),
                "o3-mini": ModelCapability(tool_calling=True, streaming=True, context_window=200000),
            },
        ))

        # 4. Anthropic Claude (Native Adapter)
        self.register(ProviderDefinition(
            id="anthropic",
            name="Anthropic Claude",
            default_base_url="https://api.anthropic.com/v1",
            default_model="claude-3-7-sonnet-20250219",
            env_key="ARCDVN_ANTHROPIC_API_KEY",
            standard_env_keys=["ANTHROPIC_API_KEY"],
            adapter_type="anthropic",
            models_hint="claude-3-7-sonnet-20250219, claude-3-5-sonnet-20241022, claude-3-5-haiku-20241022",
            models=[
                "claude-3-7-sonnet-20250219",
                "claude-3-5-sonnet-20241022",
                "claude-3-5-haiku-20241022",
                "claude-3-opus-20240229",
            ],
            capabilities={
                "claude-3-7-sonnet-20250219": ModelCapability(tool_calling=True, streaming=True, context_window=200000),
                "claude-3-5-sonnet-20241022": ModelCapability(tool_calling=True, streaming=True, context_window=200000),
                "claude-3-5-haiku-20241022": ModelCapability(tool_calling=True, streaming=True, context_window=200000),
            },
        ))

        # 5. Groq
        self.register(ProviderDefinition(
            id="groq",
            name="Groq",
            default_base_url="https://api.groq.com/openai/v1",
            default_model="llama-3.3-70b-versatile",
            env_key="ARCDVN_GROQ_API_KEY",
            standard_env_keys=["GROQ_API_KEY"],
            models_hint="llama-3.3-70b-versatile, llama-3.1-8b-instant, deepseek-r1-distill-llama-70b",
            models=[
                "llama-3.3-70b-versatile",
                "llama-3.1-8b-instant",
                "deepseek-r1-distill-llama-70b",
            ],
        ))

        # 6. DeepSeek
        self.register(ProviderDefinition(
            id="deepseek",
            name="DeepSeek",
            default_base_url="https://api.deepseek.com",
            default_model="deepseek-chat",
            env_key="ARCDVN_DEEPSEEK_API_KEY",
            standard_env_keys=["DEEPSEEK_API_KEY"],
            models_hint="deepseek-chat, deepseek-reasoner",
            models=["deepseek-chat", "deepseek-reasoner"],
        ))

        # 7. NVIDIA NIM
        self.register(ProviderDefinition(
            id="nvidia",
            name="NVIDIA NIM",
            default_base_url="https://integrate.api.nvidia.com/v1",
            default_model="meta/llama-3.3-70b-instruct",
            env_key="ARCDVN_NVIDIA_API_KEY",
            standard_env_keys=["NVIDIA_API_KEY"],
            models_hint="meta/llama-3.3-70b-instruct, deepseek-ai/deepseek-r1",
            models=[
                "meta/llama-3.3-70b-instruct",
                "deepseek-ai/deepseek-r1",
                "mistralai/mistral-large-2-instruct",
            ],
        ))

        # 8. Cerebras
        self.register(ProviderDefinition(
            id="cerebras",
            name="Cerebras",
            default_base_url="https://api.cerebras.ai/v1",
            default_model="llama3.3-70b",
            env_key="ARCDVN_CEREBRAS_API_KEY",
            standard_env_keys=["CEREBRAS_API_KEY"],
            models_hint="llama3.3-70b, llama3.1-8b",
            models=["llama3.3-70b", "llama3.1-8b"],
        ))

        # 9. Together AI
        self.register(ProviderDefinition(
            id="together",
            name="Together AI",
            default_base_url="https://api.together.xyz/v1",
            default_model="meta-llama/Llama-3.3-70B-Instruct-Turbo",
            env_key="ARCDVN_TOGETHER_API_KEY",
            standard_env_keys=["TOGETHER_API_KEY"],
            models_hint="meta-llama/Llama-3.3-70B-Instruct-Turbo, deepseek-ai/DeepSeek-V3",
            models=[
                "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                "deepseek-ai/DeepSeek-V3",
            ],
        ))

        # 10. Fireworks AI
        self.register(ProviderDefinition(
            id="fireworks",
            name="Fireworks AI",
            default_base_url="https://api.fireworks.ai/inference/v1",
            default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
            env_key="ARCDVN_FIREWORKS_API_KEY",
            standard_env_keys=["FIREWORKS_API_KEY"],
            models_hint="accounts/fireworks/models/llama-v3p3-70b-instruct, accounts/fireworks/models/deepseek-v3",
            models=[
                "accounts/fireworks/models/llama-v3p3-70b-instruct",
                "accounts/fireworks/models/deepseek-v3",
            ],
        ))

        # 11. Mistral
        self.register(ProviderDefinition(
            id="mistral",
            name="Mistral",
            default_base_url="https://api.mistral.ai/v1",
            default_model="mistral-large-latest",
            env_key="ARCDVN_MISTRAL_API_KEY",
            standard_env_keys=["MISTRAL_API_KEY"],
            models_hint="mistral-large-latest, codestral-latest, mistral-small-latest",
            models=["mistral-large-latest", "codestral-latest", "mistral-small-latest"],
        ))

        # 12. Custom OpenAI-Compatible
        self.register(ProviderDefinition(
            id="custom",
            name="Custom OpenAI-Compatible",
            default_base_url="http://localhost:11434/v1",
            default_model="llama3",
            env_key="ARCDVN_CUSTOM_API_KEY",
            standard_env_keys=["CUSTOM_API_KEY"],
            models_hint="custom model name (e.g. llama3, qwen2.5-coder:32b)",
            models=["llama3", "qwen2.5-coder"],
        ))

    def register(self, definition: ProviderDefinition) -> BaseProvider:
        if definition.adapter_type == "anthropic":
            provider = AnthropicProvider(definition)
        else:
            provider = OpenAICompatibleProvider(definition)
        self._providers[definition.id] = provider
        return provider

    def get(self, provider_id: str) -> Optional[BaseProvider]:
        norm = self.normalize_id(provider_id)
        return self._providers.get(norm)

    def list_all(self) -> List[BaseProvider]:
        return list(self._providers.values())

    def normalize_id(self, name: Any) -> str:
        if not name:
            return "google"
        p = str(name).strip().lower()
        aliases = {
            "gemini": "google",
            "google-gemini": "google",
            "google_gemini": "google",
            "open_router": "openrouter",
            "open-router": "openrouter",
            "claude": "anthropic",
            "nim": "nvidia",
            "togetherai": "together",
            "fireworksai": "fireworks",
            "local": "custom",
            "ollama": "custom",
            "lmstudio": "custom",
        }
        return aliases.get(p, p)


# Singleton registry
registry = ProviderRegistry()
