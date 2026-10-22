import json
import os
import random
import re
import time

import requests

PROVIDERS = {
    "google": {
        "id": "google",
        "name": "Google Gemini",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "default_model": "gemini-3.8-flash",
        "env_key": "ARCDVN_GOOGLE_API_KEY",
        "models_hint": "gemini-3.8-flash, gemini-2.5-flash, gemini-2.5-pro",
    },
    "openrouter": {
        "id": "openrouter",
        "name": "OpenRouter",
        "default_base_url": "https://openrouter.ai/api/v1",
        "default_model": "google/gemini-2.5-flash",
        "env_key": "ARCDVN_OPENROUTER_API_KEY",
        "models_hint": "google/gemini-2.5-flash, deepseek/deepseek-chat, meta-llama/llama-3.3-70b-instruct:free",
    },
}

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


class LLMError(RuntimeError):
    """LLM request failed with a categorized reason."""
    def __init__(self, message, *, retryable=False, status_code=None):
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


def get_config_path():
    return os.path.expanduser("~/.arcdvncli_env")


def redact_secrets(text):
    """Redact known API keys and common API token patterns from strings."""
    if not text:
        return ""
    result = str(text)

    # Redact environment-stored keys
    for var in ("ARCDVN_API_KEY", "ARCDVN_GOOGLE_API_KEY", "ARCDVN_OPENROUTER_API_KEY"):
        val = os.getenv(var)
        if val and len(val) >= 6:
            result = result.replace(val, "[REDACTED_API_KEY]")

    # Redact common credential patterns (Google AIza keys, OpenRouter sk-or keys, Bearer tokens)
    result = re.sub(r'AIza[0-9A-Za-z-_]{35}', '[REDACTED_GOOGLE_KEY]', result)
    result = re.sub(r'sk-or-v1-[0-9a-fA-F]{64}', '[REDACTED_OPENROUTER_KEY]', result)
    result = re.sub(r'Bearer\s+[A-Za-z0-9_\-\.]{8,}', 'Bearer [REDACTED_TOKEN]', result)
    return result


def normalize_provider(name):
    if not name:
        return DEFAULT_PROVIDER
    p = str(name).strip().lower()
    if p in ("gemini", "google", "google-gemini", "google_gemini"):
        return "google"
    if p in ("openrouter", "open_router", "open-router"):
        return "openrouter"
    return p


def get_active_provider():
    """Determine the active provider, respecting backward compatibility."""
    env_provider = os.getenv("ARCDVN_PROVIDER")
    if env_provider:
        return normalize_provider(env_provider)

    # Backward compatibility heuristics:
    base = (os.getenv("ARCDVN_BASE_URL") or "").lower()
    if "googleapis.com" in base or "generativelanguage" in base:
        return "google"

    # If Google key exists without OpenRouter key
    if os.getenv("ARCDVN_GOOGLE_API_KEY") and not os.getenv("ARCDVN_OPENROUTER_API_KEY"):
        return "google"

    # If legacy ARCDVN_API_KEY is present
    if os.getenv("ARCDVN_API_KEY"):
        model = os.getenv("ARCDVN_MODEL", "")
        if "gemini-3" in model or "gemini-2" in model and "/" not in model:
            return "google"
        return "openrouter"

    return DEFAULT_PROVIDER


def get_provider_config(provider=None):
    """Return dictionary with provider configuration, active key, endpoint, and model."""
    prov_id = normalize_provider(provider or get_active_provider())
    prov_def = PROVIDERS.get(prov_id, {
        "id": prov_id,
        "name": prov_id.capitalize(),
        "default_base_url": os.getenv("ARCDVN_BASE_URL", "https://openrouter.ai/api/v1"),
        "default_model": os.getenv("ARCDVN_MODEL", "google/gemini-2.5-flash"),
        "env_key": "ARCDVN_API_KEY",
        "models_hint": "",
    })

    # Key resolution: Provider-specific key -> Legacy ARCDVN_API_KEY
    prov_env_key = prov_def.get("env_key")
    key = ""
    if prov_env_key:
        key = os.getenv(prov_env_key, "").strip()
    if not key:
        key = os.getenv("ARCDVN_API_KEY", "").strip()

    # Base URL resolution
    custom_base = os.getenv("ARCDVN_BASE_URL", "").strip()
    if custom_base:
        base_url = custom_base.rstrip("/")
    else:
        base_url = prov_def["default_base_url"].rstrip("/")

    # Model resolution
    env_model = os.getenv("ARCDVN_MODEL", "").strip()
    if env_model:
        model = env_model
    else:
        model = prov_def["default_model"]

    return {
        "id": prov_id,
        "name": prov_def["name"],
        "key": key,
        "base_url": base_url,
        "model": model,
        "env_key": prov_env_key,
        "default_model": prov_def["default_model"],
        "default_base_url": prov_def["default_base_url"],
    }


def load_env():
    """Load configuration from local .env and global ~/.arcdvncli_env."""
    paths = [os.path.join(os.getcwd(), ".env"), get_config_path()]
    for p in paths:
        if not os.path.exists(p):
            continue
        is_local_env = (p != get_config_path())
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

    # Run idempotent configuration migration if needed
    _migrate_config_if_needed()


def _migrate_config_if_needed():
    """Idempotently migrate legacy configuration if only ARCDVN_API_KEY is present."""
    cfg_path = get_config_path()
    if not os.path.exists(cfg_path):
        return

    legacy_key = os.getenv("ARCDVN_API_KEY")
    provider_set = os.getenv("ARCDVN_PROVIDER")
    google_key = os.getenv("ARCDVN_GOOGLE_API_KEY")
    openrouter_key = os.getenv("ARCDVN_OPENROUTER_API_KEY")

    # If already fully configured, nothing to migrate
    if provider_set and (google_key or openrouter_key):
        return

    # If legacy key exists, map it to the right provider without deleting ARCDVN_API_KEY
    if legacy_key:
        active = get_active_provider()
        if active == "google" and not google_key:
            save_setting("ARCDVN_GOOGLE_API_KEY", legacy_key)
            if not provider_set:
                save_setting("ARCDVN_PROVIDER", "google")
        elif active == "openrouter" and not openrouter_key:
            save_setting("ARCDVN_OPENROUTER_API_KEY", legacy_key)
            if not provider_set:
                save_setting("ARCDVN_PROVIDER", "openrouter")


def save_setting(key, value):
    """Save a setting to ~/.arcdvncli_env safely with restrictive permissions."""
    p = get_config_path()
    lines = []
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()

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

    # Created with permission 600 (owner only) on Unix; ignored safely on Windows.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    os.environ[key] = str(value)


def _classify_http_error(response):
    """Return (retryable: bool, user_message: str) for a failed HTTP response with redacted secrets."""
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

    if code in _PERMANENT_STATUS:
        hints = {
            400: "Permintaan ditolak oleh provider (Bad Request). Periksa parameter model atau format payload.",
            401: "API key tidak valid atau sudah kedaluwarsa. Periksa pengaturan API Key di /settings.",
            403: "Akses ditolak atau kuota habis. Periksa saldo, langganan, atau izin model Anda.",
            404: "Model atau endpoint tidak ditemukan. Periksa nama model atau URL endpoint.",
        }
        detail = hints.get(code, err_msg)
        return False, f"LLM error {code}: {detail}"

    if code in _RETRYABLE_STATUS:
        return True, f"LLM error {code} (sementara): {err_msg}"

    return False, f"LLM error {code}: {err_msg}"


def _backoff_delay(attempt, retry_after=None):
    """Calculate delay with exponential backoff + jitter, respecting Retry-After."""
    if retry_after is not None:
        try:
            return min(float(retry_after), _BACKOFF_MAX)
        except (ValueError, TypeError):
            pass
    delay = min(_BACKOFF_BASE * (2 ** attempt), _BACKOFF_MAX)
    return delay + random.uniform(0, _JITTER_MAX)


def test_connection(provider=None, api_key=None, model=None, base_url=None):
    """
    Test LLM connection with a small read-only query.
    Returns (success: bool, message: str).
    """
    load_env()
    cfg = get_provider_config(provider)
    key = api_key or cfg["key"]
    model_name = model or cfg["model"]
    base = (base_url or cfg["base_url"]).rstrip("/")

    if not key:
        return False, f"API key untuk {cfg['name']} belum diisi."

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if cfg["id"] == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/Magelanicc/arcdvncli"
        headers["X-Title"] = "ARCDVNCLI"

    endpoint = f"{base}/chat/completions"
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
            _, msg = _classify_http_error(r)
            return False, msg

        data = r.json()
        if "choices" not in data or not data["choices"]:
            err_msg = data.get("error", {}).get("message", "Tidak ada respon 'choices'") if isinstance(data.get("error"), dict) else "Respons kosong"
            return False, redact_secrets(str(err_msg))

        return True, f"Koneksi ke {cfg['name']} ({model_name}) berhasil! Latency: {latency:.2f}s"
    except requests.exceptions.Timeout:
        return False, "Koneksi timeout setelah 20 detik. Periksa koneksi internet."
    except requests.exceptions.ConnectionError as e:
        return False, f"Gagal terhubung ke host: {redact_secrets(str(e))}"
    except Exception as e:
        return False, f"Error: {redact_secrets(str(e))}"


def chat(messages, tools=None):
    """
    Send chat completions request to active provider (Google Gemini or OpenRouter).
    Handles tool schema, error classification, bounded retries, and choice validation.
    """
    load_env()
    cfg = get_provider_config()
    key = cfg["key"]
    base = cfg["base_url"].rstrip("/")
    model = cfg["model"]

    if not key:
        env_var_hint = cfg["env_key"] or "ARCDVN_API_KEY"
        raise LLMError(
            f"API Key untuk provider '{cfg['name']}' belum disetel. "
            f"Gunakan perintah /settings atau setel {env_var_hint} di ~/.arcdvncli_env."
        )

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if cfg["id"] == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/Magelanicc/arcdvncli"
        headers["X-Title"] = "ARCDVNCLI"

    payload = {
        "model": model,
        "messages": messages,
    }

    if tools:
        payload["tools"] = [{"type": "function", "function": t} for t in tools]
        payload["tool_choice"] = "auto"

    last_error = None
    endpoint = f"{base}/chat/completions"

    for attempt in range(_MAX_RETRIES + 1):
        try:
            r = requests.post(
                endpoint,
                headers=headers,
                json=payload,
                timeout=120,
            )
        except requests.exceptions.Timeout:
            last_error = LLMError(
                "Request ke LLM timeout setelah 120 detik. Server mungkin sibuk.",
                retryable=True,
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_backoff_delay(attempt))
                continue
            raise last_error
        except requests.exceptions.ConnectionError as e:
            last_error = LLMError(
                f"Koneksi ke LLM ({cfg['name']}) gagal: {redact_secrets(str(e))}. "
                "Periksa koneksi internet atau base URL.",
                retryable=True,
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_backoff_delay(attempt))
                continue
            raise last_error
        except requests.exceptions.RequestException as e:
            raise LLMError(f"Request error: {redact_secrets(str(e))}", retryable=False)

        # --- HTTP response received ---
        if not r.ok:
            retryable, msg = _classify_http_error(r)
            last_error = LLMError(msg, retryable=retryable, status_code=r.status_code)
            if retryable and attempt < _MAX_RETRIES:
                retry_after = r.headers.get("Retry-After")
                delay = _backoff_delay(attempt, retry_after)
                time.sleep(delay)
                continue
            raise last_error

        # --- Parse JSON response ---
        try:
            data = r.json()
        except (ValueError, json.JSONDecodeError) as e:
            last_error = LLMError(
                f"Respons LLM bukan JSON valid: {e}. Body: {redact_secrets(r.text[:300])}",
                retryable=True,
            )
            if attempt < _MAX_RETRIES:
                time.sleep(_backoff_delay(attempt))
                continue
            raise last_error

        # --- Validate choices ---
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
            last_error = LLMError(
                f"API tidak mengembalikan 'choices'. Error: {redact_secrets(str(err_msg)) or '(tidak ada)'}",
                retryable=is_overload,
            )
            if is_overload and attempt < _MAX_RETRIES:
                time.sleep(_backoff_delay(attempt))
                continue
            raise last_error

        message = data["choices"][0]["message"]
        return message

    # Safety net fallback
    raise last_error or LLMError("LLM request gagal setelah semua percobaan.")