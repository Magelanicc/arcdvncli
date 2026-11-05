import json
import os
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from rich.console import Console

from .provider import (
    BaseProvider,
    LLMError,
    ModelCapability,
    ProviderDefinition,
    redact_secrets,
    registry,
)

console = Console()

# Expose PROVIDERS dictionary dynamically from registry for backward compatibility
def _build_providers_dict() -> Dict[str, Dict[str, Any]]:
    d = {}
    for p in registry.list_all():
        d[p.id] = {
            "id": p.id,
            "name": p.name,
            "default_base_url": p.definition.default_base_url,
            "default_model": p.definition.default_model,
            "env_key": p.definition.env_key,
            "models_hint": p.definition.models_hint,
        }
    return d

PROVIDERS = _build_providers_dict()
DEFAULT_PROVIDER = "google"

# ---------------------------------------------------------------------------
# Error classification & retry constants
# ---------------------------------------------------------------------------
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_PERMANENT_STATUS = {400, 401, 403, 404}
_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0   # seconds
_BACKOFF_MAX = 30.0
_JITTER_MAX = 1.0


def get_config_path() -> str:
    return os.path.expanduser("~/.arcdvncli_env")


def normalize_provider(name: Any) -> str:
    return registry.normalize_id(name)


def get_active_provider() -> str:
    """Determine the active provider, respecting backward compatibility."""
    env_provider = os.getenv("ARCDVN_PROVIDER")
    if env_provider:
        return normalize_provider(env_provider)

    # Backward compatibility heuristics:
    base = (os.getenv("ARCDVN_BASE_URL") or "").lower()
    if "googleapis.com" in base or "generativelanguage" in base:
        return "google"
    if "openrouter.ai" in base:
        return "openrouter"
    if "anthropic.com" in base:
        return "anthropic"

    # If Google key exists without OpenRouter key
    if os.getenv("ARCDVN_GOOGLE_API_KEY") and not os.getenv("ARCDVN_OPENROUTER_API_KEY"):
        return "google"

    # If OpenRouter key exists without Google key
    if os.getenv("ARCDVN_OPENROUTER_API_KEY") and not os.getenv("ARCDVN_GOOGLE_API_KEY"):
        return "openrouter"

    # If legacy ARCDVN_API_KEY is present
    if os.getenv("ARCDVN_API_KEY"):
        model = os.getenv("ARCDVN_MODEL", "")
        if "gemini-3" in model or ("gemini-2" in model and "/" not in model):
            return "google"
        return "openrouter"

    return DEFAULT_PROVIDER


def get_provider_config(provider: Optional[str] = None) -> Dict[str, Any]:
    """
    Return dictionary with provider configuration, active key, endpoint, and model.
    Enforces strict credential isolation between providers.
    """
    prov_id = normalize_provider(provider or get_active_provider())
    prov = registry.get(prov_id)

    if not prov:
        # Fallback for dynamic custom provider
        custom_def = ProviderDefinition(
            id=prov_id,
            name=prov_id.capitalize(),
            default_base_url=os.getenv("ARCDVN_BASE_URL", "http://localhost:11434/v1"),
            default_model=os.getenv("ARCDVN_MODEL", "llama3"),
            env_key=f"ARCDVN_{prov_id.upper()}_API_KEY",
            models_hint="",
        )
        prov = registry.register(custom_def)

    key = prov.get_api_key()
    base_url = prov.get_base_url()
    model = prov.get_model()

    return {
        "id": prov.id,
        "name": prov.name,
        "key": key,
        "base_url": base_url,
        "model": model,
        "env_key": prov.definition.env_key,
        "default_model": prov.definition.default_model,
        "default_base_url": prov.definition.default_base_url,
        "models_hint": prov.definition.models_hint,
    }


def load_env():
    """Load configuration from local .env and global ~/.arcdvncli_env."""
    paths = [os.path.join(os.getcwd(), ".env"), get_config_path()]
    for p in paths:
        if not os.path.exists(p):
            continue
        is_local_env = (p != get_config_path())
        try:
            with open(p, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    # Security: Only load ARCDVN_* variables
                    if not k.startswith("ARCDVN"):
                        continue
                    # Security: Local project .env MUST NEVER inject or override any API keys
                    if is_local_env and (k.endswith("_API_KEY") or "KEY" in k or "SECRET" in k or "TOKEN" in k):
                        continue
                    os.environ[k] = v.strip().strip('"').strip("'")
        except Exception:
            pass

    # Run safe, idempotent configuration migration if needed
    _migrate_config_if_needed()


def _migrate_config_if_needed():
    """
    Idempotently migrate legacy configuration if only ARCDVN_API_KEY is present.
    Strictly assigns legacy credential ONLY to the active legacy provider.
    Never allows cross-provider credential bleed.
    """
    cfg_path = get_config_path()
    if not os.path.exists(cfg_path):
        return

    legacy_key = os.getenv("ARCDVN_API_KEY", "").strip()
    if not legacy_key:
        return

    provider_set = os.getenv("ARCDVN_PROVIDER")
    google_key = os.getenv("ARCDVN_GOOGLE_API_KEY")
    openrouter_key = os.getenv("ARCDVN_OPENROUTER_API_KEY")

    # If any specific provider key is already set, migration is complete
    if google_key or openrouter_key:
        return

    # If only legacy key exists, safely map to the active legacy provider once
    active = get_active_provider()
    if active == "google" and not google_key:
        save_setting("ARCDVN_GOOGLE_API_KEY", legacy_key)
        if not provider_set:
            save_setting("ARCDVN_PROVIDER", "google")
    elif active == "openrouter" and not openrouter_key:
        save_setting("ARCDVN_OPENROUTER_API_KEY", legacy_key)
        if not provider_set:
            save_setting("ARCDVN_PROVIDER", "openrouter")


def save_setting(key: str, value: Any):
    """Save a setting to ~/.arcdvncli_env safely with restrictive permissions."""
    p = get_config_path()
    lines = []
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()
        except Exception:
            lines = []

    new_lines, found = [], False
    for line in lines:
        if not line.endswith("\n"):
            line += "\n"
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}\n")

    # Restricted permissions (0o600 on Unix)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    os.environ[key] = str(value)


def _backoff_delay(attempt: int, retry_after: Optional[Any] = None) -> float:
    """Calculate delay with exponential backoff + jitter, respecting Retry-After."""
    if retry_after is not None:
        try:
            return min(max(float(retry_after), 0.5), _BACKOFF_MAX)
        except (ValueError, TypeError):
            import email.utils
            try:
                parsed = email.utils.parsedate_to_datetime(str(retry_after))
                diff = parsed.timestamp() - time.time()
                if diff > 0:
                    return min(diff, _BACKOFF_MAX)
            except Exception:
                pass
    delay = min(_BACKOFF_BASE * (1.8 ** attempt), _BACKOFF_MAX)
    return delay + random.uniform(0, _JITTER_MAX)


def test_connection(
    provider: Optional[str] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    base_url: Optional[str] = None,
) -> Tuple[bool, str]:
    """
    Test LLM connection with a small read-only query.
    Returns (success: bool, message: str).
    """
    load_env()
    prov_id = normalize_provider(provider or get_active_provider())
    prov = registry.get(prov_id)
    if not prov:
        return False, f"Provider '{prov_id}' tidak dikenal."

    return prov.test_connection(api_key=api_key, model=model, base_url=base_url)


# ---------------------------------------------------------------------------
# Task-level usage and attempt metrics for observability
# ---------------------------------------------------------------------------
_TASK_PROMPT_TOKENS = 0
_TASK_COMPLETION_TOKENS = 0
_TASK_TOTAL_TOKENS = 0
_TASK_HTTP_ATTEMPTS = 0
_TASK_INTERNAL_RETRIES = 0
_HAS_PROVIDER_USAGE = False
_LAST_USAGE = None


def reset_llm_metrics():
    """Reset per-task LLM usage metrics."""
    global _TASK_PROMPT_TOKENS, _TASK_COMPLETION_TOKENS, _TASK_TOTAL_TOKENS
    global _TASK_HTTP_ATTEMPTS, _TASK_INTERNAL_RETRIES, _HAS_PROVIDER_USAGE, _LAST_USAGE
    _TASK_PROMPT_TOKENS = 0
    _TASK_COMPLETION_TOKENS = 0
    _TASK_TOTAL_TOKENS = 0
    _TASK_HTTP_ATTEMPTS = 0
    _TASK_INTERNAL_RETRIES = 0
    _HAS_PROVIDER_USAGE = False
    _LAST_USAGE = None


def get_llm_task_metrics() -> Dict[str, Any]:
    """Return aggregated LLM usage and attempt metrics for the active task."""
    return {
        "has_usage": _HAS_PROVIDER_USAGE,
        "prompt_tokens": _TASK_PROMPT_TOKENS if _HAS_PROVIDER_USAGE else None,
        "completion_tokens": _TASK_COMPLETION_TOKENS if _HAS_PROVIDER_USAGE else None,
        "total_tokens": _TASK_TOTAL_TOKENS if _HAS_PROVIDER_USAGE else None,
        "http_attempts": _TASK_HTTP_ATTEMPTS,
        "internal_retries": _TASK_INTERNAL_RETRIES,
        "last_usage": dict(_LAST_USAGE) if _LAST_USAGE else None,
    }


def chat(messages: List[Dict[str, Any]], tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    Send chat completions request to active provider.
    Handles error classification, bounded retries with jitter, usage accumulation,
    and optional fallback provider.
    """
    global _TASK_HTTP_ATTEMPTS, _TASK_INTERNAL_RETRIES
    global _HAS_PROVIDER_USAGE, _LAST_USAGE, _TASK_PROMPT_TOKENS, _TASK_COMPLETION_TOKENS, _TASK_TOTAL_TOKENS

    load_env()
    active_prov_id = get_active_provider()
    provider = registry.get(active_prov_id)

    if not provider:
        raise LLMError(f"Provider '{active_prov_id}' tidak ditemukan dalam registry.")

    last_error = None

    for attempt in range(_MAX_RETRIES + 1):
        _TASK_HTTP_ATTEMPTS += 1
        if attempt > 0:
            _TASK_INTERNAL_RETRIES += 1

        try:
            msg = provider.chat(messages, tools=tools)

            # Record usage
            usage = msg.get("_usage") if isinstance(msg, dict) else None
            if usage and isinstance(usage, dict):
                _HAS_PROVIDER_USAGE = True
                _LAST_USAGE = dict(usage)
                p_tok = usage.get("prompt_tokens")
                c_tok = usage.get("completion_tokens")
                t_tok = usage.get("total_tokens")
                if p_tok is not None:
                    try:
                        _TASK_PROMPT_TOKENS += int(p_tok)
                    except (ValueError, TypeError):
                        pass
                if c_tok is not None:
                    try:
                        _TASK_COMPLETION_TOKENS += int(c_tok)
                    except (ValueError, TypeError):
                        pass
                if t_tok is not None:
                    try:
                        _TASK_TOTAL_TOKENS += int(t_tok)
                    except (ValueError, TypeError):
                        pass

            return msg

        except LLMError as e:
            last_error = e
            if not e.retryable:
                raise e

            if attempt < _MAX_RETRIES:
                retry_after = getattr(e, "retry_after", None)
                delay = _backoff_delay(attempt, retry_after=retry_after)
                console.print(
                    f"[bold yellow]⏳ Provider sibuk ({e}), mencoba ulang ({attempt + 1}/{_MAX_RETRIES}) dalam {delay:.1f}s...[/bold yellow]"
                )
                time.sleep(delay)
                continue
            break

    # Check for configured fallback provider
    fallback_id = os.getenv("ARCDVN_FALLBACK_PROVIDER", "").strip()
    if fallback_id and fallback_id != active_prov_id:
        fallback_prov = registry.get(fallback_id)
        if fallback_prov and fallback_prov.get_api_key():
            console.print(
                f"[bold yellow]⚠️ Provider utama '{provider.name}' tidak merespons. "
                f"Beralih ke fallback '{fallback_prov.name}'...[/bold yellow]"
            )
            try:
                _TASK_HTTP_ATTEMPTS += 1
                msg = fallback_prov.chat(messages, tools=tools)
                return msg
            except Exception as fb_err:
                console.print(f"[bold red]Fallback ke '{fallback_prov.name}' juga gagal: {fb_err}[/bold red]")

    raise last_error or LLMError("LLM request gagal setelah semua percobaan.")
