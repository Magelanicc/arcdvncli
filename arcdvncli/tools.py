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


def edit_file(path, old_str=None, new_str=None, replace_all=False, **kwargs):
    # Fallback to alternate param names if LLM uses variations
    if old_str is None:
        for k in ("old_text", "old_content", "target", "search"):
            if k in kwargs:
                old_str = kwargs[k]
                break
    if new_str is None:
        for k in ("new_text", "new_content", "replacement", "replace"):
            if k in kwargs:
                new_str = kwargs[k]
                break

    if old_str is None:
        raise ValueError("Parameter 'old_str' wajib diisi.")
    if new_str is None:
        raise ValueError("Parameter 'new_str' wajib diisi.")
    if not old_str:
        raise ValueError("'old_str' tidak boleh berupa string kosong.")

    p = safe_path(path)
    if not p.exists():
        raise FileNotFoundError(f"File tidak ditemukan: {path}. Gunakan write_file jika ingin membuat file baru.")
    if not p.is_file():
        raise IsADirectoryError(f"Path adalah direktori: {path}")
    if _is_sensitive(p):
        raise PermissionError(f"{p.name} berisi data sensitif, tidak boleh diedit agent.")

    original_text = p.read_text(encoding="utf-8", errors="replace")

    # 1. Cek kecocokan langsung (exact match)
    if old_str in original_text:
        count = original_text.count(old_str)
        if count > 1 and not replace_all:
            return (
                f"Gagal mengedit {p.relative_to(ROOT)}: 'old_str' ditemukan sebanyak {count} kali di dalam file. "
                "Sertakan lebih banyak baris konteks di sekitarnya agar unik."
            )
        new_content = original_text.replace(old_str, new_str, -1 if replace_all else 1)
    else:
        # 2. Coba normalisasi line ending (\r\n vs \n) jika format baris berbeda
        crlf_file = "\r\n" in original_text
        norm_text = original_text.replace("\r\n", "\n")
        norm_old = old_str.replace("\r\n", "\n")
        norm_new = new_str.replace("\r\n", "\n")

        count = norm_text.count(norm_old)
        if count == 0:
            return (
                f"Gagal mengedit {p.relative_to(ROOT)}: Teks 'old_str' tidak ditemukan di dalam file. "
                "Pastikan indentasi, spasi, dan baris kode persis sama dengan isi file. "
                "Gunakan read_file untuk memeriksa isi file terlebih dahulu."
            )
        if count > 1 and not replace_all:
            return (
                f"Gagal mengedit {p.relative_to(ROOT)}: 'old_str' ditemukan sebanyak {count} kali di dalam file. "
                "Sertakan lebih banyak baris konteks di sekitarnya agar unik."
            )

        norm_result = norm_text.replace(norm_old, norm_new, -1 if replace_all else 1)
        new_content = norm_result.replace("\n", "\r\n") if crlf_file else norm_result

    if new_content == original_text:
        return f"Tidak ada perubahan pada {p.relative_to(ROOT)}: old_str dan new_str menghasilkan teks yang sama."

    rel_path = str(p.relative_to(ROOT))
    _show_diff(original_text, new_content, rel_path)

    if not _confirm(f"Terapkan perubahan pada: {escape(rel_path)}?"):
        console.print("[red]Perubahan file dibatalkan oleh user.[/red]")
        return "Edit rejected by user."

    p.write_text(new_content, encoding="utf-8")
    return f"edited {rel_path}"


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
    {"name":"edit_file","description":"Edit an existing file by replacing a specific block of text (old_str) with new text (new_str). This is much faster, token-efficient, and preferred over write_file for modifying existing files. old_str must uniquely match the code in the file.",
     "parameters":{"type":"object","properties":{"path":{"type":"string","description":"Relative path to the file to edit"},"old_str":{"type":"string","description":"Exact text block to find and replace"},"new_str":{"type":"string","description":"New replacement text to insert"}},"required":["path","old_str","new_str"]}},
    {"name":"write_file","description":"Create a new text file or completely overwrite an existing file. For modifying existing files, always prefer edit_file instead.",
     "parameters":{"type":"object","properties":{"path":{"type":"string"},"content":{"type":"string"}},"required":["path","content"]}},
    {"name":"run_command","description":"Run a shell command in the current project. User approval is required.",
     "parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}},
]

FUNCTIONS = {
    "list_files": list_files,
    "read_file": read_file,
    "edit_file": edit_file,
    "write_file": write_file,
    "run_command": run_command,
}