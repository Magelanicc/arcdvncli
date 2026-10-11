import difflib
import os
import subprocess
from pathlib import Path

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

ROOT = Path.cwd().resolve()
IGNORED = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}
SENSITIVE_SUFFIXES = {
    ".pem", ".key", ".pfx", ".p12", ".pkcs12",
    ".jks", ".keystore", ".kdbx", ".ovpn",
}
SENSITIVE_NAMES = {
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    ".npmrc", ".pypirc", ".netrc", ".dockercfg",
    ".pgpass", ".htpasswd", "credentials",
    "service-account.json", "service_account.json",
}
MAX_FILES = 300

console = Console()


def safe_path(path):
    p = (ROOT / path).resolve()
    if ROOT != p and ROOT not in p.parents:
        raise ValueError("Path di luar project ditolak.")
    return p


def _is_sensitive(p):
    # 1. Lindungi isi folder .git (termasuk .git/config yang bisa memuat personal access token)
    if any(part == ".git" for part in p.parts):
        return True

    name = p.name.lower()
    # 2. Lindungi file environment (.env, .env.local, .envrc, dsb., kecuali contoh seperti .env.example)
    if name == ".env" or name == ".envrc" or (
        name.startswith(".env.") and not (name.endswith(".example") or name.endswith(".sample"))
    ):
        return True

    # 3. Lindungi SSH private keys (misal id_rsa, id_ed25519; izinkan public key berakhiran .pub)
    if name.startswith("id_") and not name.endswith(".pub"):
        return True

    # 4. Lindungi file dengan nama atau ekstensi sensitif (token, cert, keystore)
    return name in SENSITIVE_NAMES or p.suffix.lower() in SENSITIVE_SUFFIXES


def _confirm(message):
    console.print(f"[dim]{message} (y/N): [/dim]", end="")
    return input().strip().lower() == "y"


def _show_diff(old_text, new_text, filename):
    diff = list(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{filename}",
            tofile=f"b/{filename}",
            n=3,
        )
    )
    if diff:
        diff_text = "".join(diff)
        console.print(
            Panel(
                Syntax(diff_text, "diff", theme="monokai", line_numbers=False),
                title=f"[bold yellow]Changes preview: {filename}[/bold yellow]",
                border_style="yellow",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )


def list_files(path="."):
    base = safe_path(path)
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        # Prune folder ignored SEBELUM masuk (cepat, dan tidak terpengaruh path induk)
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED)
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(ROOT)
            out.append(rel.as_posix())
            if len(out) >= MAX_FILES:
                return "\n".join(out) + f"\n... (dipotong di {MAX_FILES} file)"
    return "\n".join(out) or "(empty)"


def read_file(path):
    p = safe_path(path)
    if not p.is_file():
        raise FileNotFoundError(path)
    if _is_sensitive(p):
        raise PermissionError(f"{p.name} berisi data sensitif, tidak boleh dibaca agent.")
    text = p.read_text(encoding="utf-8", errors="replace")
    return text[:30000]


def write_file(path, content):
    p = safe_path(path)
    if p.exists():
        if not p.is_file():
            raise IsADirectoryError(path)
        try:
            old_text = p.read_text(encoding="utf-8", errors="replace")
            _show_diff(old_text, content, str(p.relative_to(ROOT)))
        except Exception:
            pass
        if not _confirm(f"Timpa file yang sudah ada: {escape(str(p.relative_to(ROOT)))}?"):
            console.print("[red]Penimpaan file dibatalkan oleh user.[/red]")
            return "Write rejected by user."
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {p.relative_to(ROOT)}"


def _command_preview(command):
    command = " ".join(command.split())
    return command if len(command) <= 120 else command[:117] + "..."


def run_command(command):
    cmd_display = Syntax(command.strip(), "bash", theme="monokai", word_wrap=True)
    console.print(
        Panel(
            cmd_display,
            title="[bold yellow]⚠ Permission required[/bold yellow]",
            subtitle="[dim]ARCDVN wants to execute this command[/dim]",
            border_style="yellow",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )
    if not _confirm("Izinkan eksekusi?"):
        console.print("[red]Perintah dibatalkan oleh user.[/red]")
        return "Command rejected by user."

    try:
        with console.status("[bold yellow]Menjalankan perintah...[/bold yellow]", spinner="dots"):
            r = subprocess.run(
                command, shell=True, cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
    except subprocess.TimeoutExpired:
        return "Command timed out after 120s."

    return f"exit_code={r.returncode}\nSTDOUT:\n{r.stdout[-12000:]}\nSTDERR:\n{r.stderr[-12000:]}"



TOOL_DEFS = [
    {"name":"list_files","description":"List project files recursively. Use this to inspect project structure.",
     "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":[]}},
    {"name":"read_file","description":"Read a text file from the current project.",
     "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
    {"name":"write_file","description":"Create or overwrite a text file in the current project. Overwriting needs user approval.",
     "parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}},
    {"name":"run_command","description":"Run a shell command in the current project. User approval is required.",
     "parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}},
]

FUNCTIONS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "run_command": run_command,
}