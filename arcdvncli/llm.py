import json
import os
import requests

def get_config_path():
    return os.path.expanduser("~/.arcdvncli_env")

def load_env():
    # Keamanan: Muat .env lokal terlebih dahulu, lalu timpa dengan ~/.arcdvncli_env (konfigurasi global).
    # Konfigurasi global milik user SELALU memiliki prioritas tertinggi di atas .env lokal proyek.
    # Selain itu, file .env lokal proyek TIDAK BOLEH menimpa atau menyusupkan ARCDVN_API_KEY.
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
                # Hanya ambil setting milik ARCDVN, jangan impor .env milik project user
                if not k.startswith("ARCDVN"):
                    continue
                # Keamanan: .env repositori lokal tidak diizinkan menyusupkan ARCDVN_API_KEY
                if is_local_env and k == "ARCDVN_API_KEY":
                    continue
                os.environ[k] = v.strip().strip('"').strip("'")


def save_setting(key, value):
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

    # Dibuat langsung dengan permission 600 (hanya pemilik) di Linux/macOS.
    # Di Windows mode ini diabaikan, tidak berbahaya.
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(new_lines)
    try:
        os.chmod(p, 0o600)  # kalau file lama sudah terlanjur 644
    except OSError:
        pass
    os.environ[key] = value


def chat(messages, tools):
    load_env()
    key = os.getenv("ARCDVN_API_KEY")
    base = os.getenv("ARCDVN_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    model = os.getenv("ARCDVN_MODEL", "google/gemini-2.5-flash")
    if not key:
        raise RuntimeError("ARCDVN_API_KEY belum di-set. Buat file .env dari .env.example.")

    payload = {
        "model": model,
        "messages": messages,
        "tools": [{"type": "function", "function": t} for t in tools],
        "tool_choice": "auto",
    }
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload, timeout=120
    )
    if not r.ok:
        raise RuntimeError(f"LLM error {r.status_code}: {r.text[:1000]}")

    data = r.json()

    if "choices" not in data or not data["choices"]:
        # Beberapa kemungkinan: model overloaded, format tidak didukung, dsb.
        err_msg = data.get("error", {}).get("message", "") if isinstance(data.get("error"), dict) else str(data.get("error", ""))
        preview = json.dumps(data, indent=2, ensure_ascii=False)[:800]
        raise RuntimeError(
            f"API tidak mengembalikan 'choices'.\n"
            f"  Error: {err_msg or '(tidak ada)'}\n"
            f"  Response: {preview}"
        )

    return data["choices"][0]["message"]