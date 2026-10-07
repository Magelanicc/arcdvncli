import os
import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

ROOT = Path.cwd().resolve()
IGNORED = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build"}

console = Console()


def safe_path(path):
    p = (ROOT / path).resolve()
    if ROOT != p and ROOT not in p.parents:
        raise ValueError("Path di luar project ditolak.")
    return p


def list_files(path="."):
    base = safe_path(path)
    out = []
    for p in base.rglob("*"):
        if any(part in IGNORED for part in p.parts):
            continue
        if p.is_file():
            out.append(str(p.relative_to(ROOT)))
        if len(out) >= 300:
            break
    return "\n".join(out) or "(empty)"


def read_file(path):
    p = safe_path(path)
    if not p.is_file():
        raise FileNotFoundError(path)
    text = p.read_text(encoding="utf-8", errors="replace")
    return text[:30000]


def write_file(path, content):
    p = safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {p.relative_to(ROOT)}"


def _command_preview(command):
    command = " ".join(command.split())
    return command if len(command) <= 120 else command[:117] + "..."


def run_command(command):
    console.print(
        Panel(
            Text(command, style="bold white"),
            title="⚠ Permission required",
            subtitle="ARCDVN wants to execute this command",
            border_style="yellow",
        )
    )
    console.print("[dim]Izinkan eksekusi? (y/N): [/dim]", end="")
    answer = input().strip().lower()
    if answer != "y":
        console.print("[red]Perintah dibatalkan oleh user.[/red]")
        return "Command rejected by user."

    with console.status("[bold yellow]Menjalankan perintah...[/bold yellow]", spinner="bouncingBar"):
        r = subprocess.run(command, shell=True, cwd=ROOT, capture_output=True, text=True, timeout=120)

    return f"exit_code={r.returncode}\nSTDOUT:\n{r.stdout[-12000:]}\nSTDERR:\n{r.stderr[-12000:]}"

TOOL_DEFS = [
    {"name":"list_files","description":"List project files recursively. Use this to inspect project structure.",
     "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":[]}},
    {"name":"read_file","description":"Read a text file from the current project.",
     "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}},
    {"name":"write_file","description":"Create or overwrite a text file in the current project.",
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
