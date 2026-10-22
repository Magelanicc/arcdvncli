"""Smoke test lintas OS. Jalankan dari root repo: python tests/smoke_test.py"""
import builtins
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Isolasi: HOME palsu + project palsu di dalam folder bernama "build" (kasus bug lama)
home = tempfile.mkdtemp()
os.environ["HOME"] = home
os.environ["USERPROFILE"] = home
project = Path(tempfile.mkdtemp()) / "build" / "myproj"
(project / "node_modules" / "x").mkdir(parents=True)
(project / "src").mkdir()
for name in ["a.txt", "src/main.py", "node_modules/x/junk.js", ".env", "key.pem", ".env.example"]:
    (project / name).write_text("secret=1", encoding="utf-8")
os.chdir(project)

from arcdvncli import __version__, agent, cli, llm, tools, updater  # noqa: E402

FAILED = []


def check(label, cond):
    print(("PASS" if cond else "FAIL"), "-", label)
    if not cond:
        FAILED.append(label)


# --- tools.list_files
listed = {x.replace("\\", "/") for x in tools.list_files().split("\n")}
check("list_files jalan walau path project mengandung folder 'build'",
      listed == {".env", ".env.example", "a.txt", "key.pem", "src/main.py"})
check("list_files melewati node_modules", "node_modules/x/junk.js" not in listed)

# --- tools.read_file
def blocked(f):
    try:
        tools.read_file(f)
        return False
    except PermissionError:
        return True

check("read_file memblokir .env dan *.pem", blocked(".env") and blocked("key.pem"))
check("read_file mengizinkan .env.example dan file biasa",
      not blocked(".env.example") and not blocked("a.txt"))
try:
    tools.read_file("../../etc/passwd")
    check("safe_path menolak path di luar project", False)
except (ValueError, FileNotFoundError) as e:
    check("safe_path menolak path di luar project", isinstance(e, ValueError))

# --- tools.write_file
answers = iter(["n", "y"])
builtins.input = lambda *a: next(answers)
check("write_file file baru tanpa konfirmasi", tools.write_file("baru.txt", "x").startswith("wrote"))
check("write_file timpa + jawab n -> ditolak",
      tools.write_file("a.txt", "NEW") == "Write rejected by user."
      and (project / "a.txt").read_text() == "secret=1")
check("write_file timpa + jawab y -> berhasil",
      tools.write_file("a.txt", "NEW").startswith("wrote") and (project / "a.txt").read_text() == "NEW")

# --- tools.edit_file
(project / "code.py").write_text("def hello():\n    print('lama')\n    return 42\n", encoding="utf-8")
answers = iter(["n", "y"])
builtins.input = lambda *a: next(answers)
check("edit_file tolak saat jawab n",
      tools.edit_file("code.py", "print('lama')", "print('baru')") == "Edit rejected by user."
      and "print('lama')" in (project / "code.py").read_text())
check("edit_file berhasil saat jawab y (hanya baris target berubah)",
      tools.edit_file("code.py", "print('lama')", "print('baru')").startswith("edited")
      and "print('baru')" in (project / "code.py").read_text()
      and "return 42" in (project / "code.py").read_text())
check("edit_file target tidak ditemukan",
      "tidak ditemukan" in tools.edit_file("code.py", "baris_gaib", "x"))
(project / "ambigu.txt").write_text("duplikat\nduplikat\n", encoding="utf-8")
check("edit_file target ambigu (>1) ditolak",
      "ditemukan sebanyak 2 kali" in tools.edit_file("ambigu.txt", "duplikat", "x"))
try:
    tools.edit_file(".env", "a", "b")
    check("edit_file memblokir file sensitif", False)
except PermissionError:
    check("edit_file memblokir file sensitif", True)

# --- tools.run_command
builtins.input = lambda *a: "y"
out = tools.run_command("echo halo")
check("run_command menjalankan perintah", "exit_code=0" in out and "halo" in out)
builtins.input = lambda *a: "n"
check("run_command ditolak user", tools.run_command("echo x") == "Command rejected by user.")
builtins.input = lambda *a: "y"
_orig_run = subprocess.run
def _timeout(*a, **k):
    raise subprocess.TimeoutExpired("x", 1)
subprocess.run = _timeout
check("run_command timeout tidak crash", "timed out" in tools.run_command("sleep 999"))
subprocess.run = _orig_run

# --- llm.save_setting / load_env
cfg = llm.get_config_path()
Path(cfg).write_text("ARCDVN_MODEL=old/model", encoding="utf-8")  # sengaja tanpa newline
llm.save_setting("ARCDVN_API_KEY", "sk-test-123")
llm.save_setting("ARCDVN_MODEL", "new/model")
check("save_setting menjaga format baris",
      Path(cfg).read_text(encoding="utf-8") == "ARCDVN_MODEL=new/model\nARCDVN_API_KEY=sk-test-123\n")
if os.name != "nt":
    check("config file permission 600", (os.stat(cfg).st_mode & 0o777) == 0o600)
(project / ".git").mkdir(exist_ok=True)
(project / ".git" / "config").write_text("token=secret", encoding="utf-8")
(project / "id_rsa").write_text("private", encoding="utf-8")
(project / "id_rsa.pub").write_text("public", encoding="utf-8")
check("read_file memblokir .git/config dan id_rsa", blocked(".git/config") and blocked("id_rsa"))
check("read_file mengizinkan public key .pub", not blocked("id_rsa.pub"))

(project / ".env").write_text("DATABASE_URL=postgres://x\nARCDVN_BASE_URL=https://example.com/v1\nARCDVN_API_KEY=evil_key\n", encoding="utf-8")
os.environ.pop("DATABASE_URL", None)
llm.load_env()
check("load_env hanya mengambil key ARCDVN*",
      "DATABASE_URL" not in os.environ and os.environ.get("ARCDVN_BASE_URL") == "https://example.com/v1")
check("load_env mencegah .env lokal menimpa ARCDVN_API_KEY",
      os.environ.get("ARCDVN_API_KEY") == "sk-test-123")

# --- updater
class _R:
    def __init__(self, tag): self.tag = tag
    def raise_for_status(self): pass
    def json(self): return {"tag_name": self.tag}

def _fake(tag):
    return lambda url, timeout: _R(tag)

def _fresh():
    p = updater._cache_path()
    if os.path.exists(p):
        os.remove(p)

os.environ.pop("ARCDVN_NO_UPDATE_CHECK", None)
updater.__version__ = "0.1.1"
check("_parse: 0.1.10 > 0.1.9", updater._parse("v0.1.10") > updater._parse("0.1.9"))
_fresh(); updater.requests.get = _fake("v0.1.2")
msg = updater.check_for_update()
check("updater: ada versi baru -> pesan muncul (pipx + tag)", bool(msg) and "pipx install" in msg and "@v0.1.2" in msg)
sys.frozen = True
check("updater: binary -> arahkan ke Releases", "releases/latest" in updater.check_for_update())
del sys.frozen
_fresh(); updater.requests.get = _fake("v0.1.1")
check("updater: versi sama -> diam", updater.check_for_update() is None)
def _boom(*a, **k): raise ConnectionError("offline")
_fresh(); updater.requests.get = _boom
check("updater: offline -> diam", updater.check_for_update() is None)

# --- agent
calls = iter([
    {"role": "assistant", "content": None,
     "tool_calls": [{"id": "1", "function": {"name": "list_files", "arguments": "{rusak"}}]},
    {"role": "assistant", "content": "selesai"},
])
agent.chat = lambda m, t: next(calls)
check("agent: argumen JSON rusak tidak bikin crash", agent.run_agent("tes", []) == "selesai")
check("agent: system prompt memuat info OS", "Operating system:" in agent._system_prompt())

# --- cli (logo + markup tidak error)
os.system = lambda c: 0
try:
    cli.banner("test/model")
    cli._command_help()
    check("cli: banner + help ter-render tanpa error", True)
except Exception as e:
    check(f"cli: banner + help ter-render tanpa error ({type(e).__name__}: {e})", False)

print()
print("Versi:", __version__, "| Python:", sys.version.split()[0], "| OS:", os.name)
if __name__ == "__main__":
    sys.exit(1 if FAILED else 0)