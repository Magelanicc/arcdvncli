import json
import os
import requests

def get_config_path():
    return os.path.expanduser("~/.arcdvncli_env")

def load_env():
    paths = [get_config_path(), os.path.join(os.getcwd(), ".env")]
    for p in paths:
        if os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip().strip('"').strip("'")

def save_setting(key, value):
    p = get_config_path()
    lines = []
    if os.path.exists(p):
        lines = open(p, encoding="utf-8").readlines()
    
    found = False
    with open(p, "w", encoding="utf-8") as f:
        for line in lines:
            if line.strip().startswith(f"{key}="):
                f.write(f"{key}={value}\n")
                found = True
            else:
                f.write(line)
        if not found:
            f.write(f"{key}={value}\n")
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
