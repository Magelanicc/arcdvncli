import difflib
import os
import subprocess
import sys
from pathlib import Path

from rich import box
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

ROOT = Path.cwd().resolve()
IGNORED = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build",
    ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
    ".turbo", ".nuxt", ".output", "out", "coverage", ".dart_tool", "vendor", "target",
    "Pods", ".gradle", "bin", "obj",
}
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

# In-turn caches for targeted performance
_DIR_CACHE = {}
_FILE_CACHE = {}

# ---------------------------------------------------------------------------
# Cache metrics — reset per task via reset_task_metrics()
# ---------------------------------------------------------------------------
_CACHE_HITS = 0
_CACHE_MISSES = 0
_READ_COUNT = 0
_REPEATED_READ_COUNT = 0
_OVERLAPPING_READ_COUNT = 0
_TOOL_RESULT_BYTES = 0
_TOOL_COUNTS = {}               # tool_name -> call count
_READS_THIS_TASK = {}           # (path_str, start_line, end_line) -> True
_FILE_RANGES_THIS_TASK = {}     # path_str -> list of (start_line, end_line)


def reset_task_metrics():
    """Reset per-task metrics and flush stale directory caches. Called at the start of each agent run."""
    global _CACHE_HITS, _CACHE_MISSES, _READ_COUNT, _REPEATED_READ_COUNT, _OVERLAPPING_READ_COUNT
    global _TOOL_RESULT_BYTES, _TOOL_COUNTS, _READS_THIS_TASK, _FILE_RANGES_THIS_TASK, _DIR_CACHE
    _CACHE_HITS = 0
    _CACHE_MISSES = 0
    _READ_COUNT = 0
    _REPEATED_READ_COUNT = 0
    _OVERLAPPING_READ_COUNT = 0
    _TOOL_RESULT_BYTES = 0
    _TOOL_COUNTS = {}
    _READS_THIS_TASK = {}
    _FILE_RANGES_THIS_TASK = {}
    _DIR_CACHE.clear()  # Ensure each new user turn sees fresh directory listings


def get_task_metrics():
    """Return a snapshot of per-task tool and cache metrics."""
    return {
        "cache_hits": _CACHE_HITS,
        "cache_misses": _CACHE_MISSES,
        "read_count": _READ_COUNT,
        "repeated_read_count": _REPEATED_READ_COUNT,
        "overlapping_read_count": _OVERLAPPING_READ_COUNT,
        "tool_result_bytes": _TOOL_RESULT_BYTES,
        "tool_counts": dict(_TOOL_COUNTS),
    }


def record_tool_call(tool_name):
    """Increment the call count for a specific tool."""
    global _TOOL_COUNTS
    _TOOL_COUNTS[tool_name] = _TOOL_COUNTS.get(tool_name, 0) + 1


def record_tool_result_bytes(val):
    """Add bytes to the tool result total (measured in UTF-8 bytes)."""
    global _TOOL_RESULT_BYTES
    if isinstance(val, (int, float)):
        _TOOL_RESULT_BYTES += int(val)
    else:
        _TOOL_RESULT_BYTES += len(str(val or "").encode("utf-8"))


def invalidate_caches(path=None):
    """Invalidate filesystem in-memory caches when changes occur."""
    global _DIR_CACHE, _FILE_CACHE, _READS_THIS_TASK, _FILE_RANGES_THIS_TASK
    _DIR_CACHE.clear()
    if path:
        p_str = str(path)
        try:
            resolved_str = str(safe_path(path))
        except Exception:
            resolved_str = p_str
        targets = {p_str, resolved_str}
        _FILE_CACHE = {k: v for k, v in _FILE_CACHE.items() if k[0] not in targets}
        # Invalidate read tracking for this file so re-reads after edits are not flagged as repeated
        _READS_THIS_TASK = {k: v for k, v in _READS_THIS_TASK.items() if k[0] not in targets}
        _FILE_RANGES_THIS_TASK = {k: v for k, v in _FILE_RANGES_THIS_TASK.items() if k not in targets}
    else:
        _FILE_CACHE.clear()
        _READS_THIS_TASK.clear()
        _FILE_RANGES_THIS_TASK.clear()


def safe_path(path):
    if not path or not str(path).strip():
        raise ValueError("Path tidak boleh kosong.")
    path_str = str(path).strip()
    if "\x00" in path_str:
        raise ValueError("Path mengandung karakter null byte yang tidak valid.")

    p = (ROOT / path_str).resolve()
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


def _stop_active_spinners():
    """Safely stop any active Rich Live or Status instances so input/prompts are never overwritten."""
    consoles = [console]
    try:
        if "arcdvncli.agent" in sys.modules:
            ag = sys.modules["arcdvncli.agent"]
            if hasattr(ag, "console") and ag.console not in consoles:
                consoles.append(ag.console)
        if "arcdvncli.cli" in sys.modules:
            cl = sys.modules["arcdvncli.cli"]
            if hasattr(cl, "console") and cl.console not in consoles:
                consoles.append(cl.console)
    except Exception:
        pass

    for c in consoles:
        if hasattr(c, "_live_stack"):
            for live in list(c._live_stack):
                try:
                    live.stop()
                except Exception:
                    pass
        try:
            c.show_cursor(True)
        except Exception:
            pass


def _flush_pending_input():
    """Flush stale keystrokes from stdin to avoid accidental confirmations."""
    try:
        if sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            if os.name == "nt":
                import msvcrt
                while msvcrt.kbhit():
                    msvcrt.getch()
            else:
                import termios
                try:
                    termios.tcflush(sys.stdin, termios.TCIFLUSH)
                except Exception:
                    pass
    except Exception:
        pass


def confirm_action(prompt="Izinkan tindakan ini?", action_title="Konfirmasi Diperlukan", details=None):
    """
    Unified global confirmation prompt for sensitive actions across ARCDVNCLI.

    Guarantees:
    - Suspends any active status spinner / live redraws and restores terminal cursor.
    - Flushes stale input in stdin.
    - Displays a visible pending-confirmation prompt with explicit [Y] Approve and [N] Deny options.
    - Captures user input and supports lowercase & uppercase decisions ('y', 'Y', 'yes', 'n', 'N', 'no', 'tidak').
    - Reprompts with helpful instructions on invalid input without executing anything.
    - Safely handles EOF, Ctrl+C (KeyboardInterrupt), and cancellation as default-deny without crashing.
    - Returns exactly one boolean decision (True = approved, False = denied).
    """
    _stop_active_spinners()
    _flush_pending_input()

    panel_content = [
        "[bold yellow]⏳ Menunggu persetujuan user... / Pending user confirmation[/bold yellow]",
    ]
    if details:
        panel_content.append(f"[dim]{details}[/dim]")
    panel_content.append(
        "[dim]Pilihan: [/dim][bold green][Y] Approve (Setuju)[/bold green]  [dim]•[/dim]  "
        "[bold red][N] Deny (Tolak)[/bold red]  [dim]•  (Ctrl+C: Batalkan)[/dim]"
    )

    console.print(
        Panel(
            "\n".join(panel_content),
            title=f"[bold yellow]⚠ {action_title}[/bold yellow]",
            border_style="yellow",
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )

    while True:
        try:
            console.print(f"[bold cyan]{escape(prompt)} \\[y/N]: [/bold cyan]", end="")
            sys.stdout.flush()
            raw = input()
        except KeyboardInterrupt:
            console.print("\n[yellow]⚠ Tindakan dibatalkan oleh user (Ctrl+C).[/yellow]")
            return False
        except EOFError:
            console.print("\n[yellow]⚠ EOF terdeteksi. Tindakan ditolak secara default.[/yellow]")
            return False
        except Exception as e:
            console.print(f"\n[red]Error membaca input: {e}. Tindakan ditolak.[/red]")
            return False

        clean = raw.strip().lower()

        if clean in ("y", "yes", "ya", "sim", "1"):
            console.print("[green]✓ Tindakan disetujui.[/green]")
            return True
        elif clean in ("n", "no", "tidak", "t", "0", ""):
            console.print("[yellow]✗ Tindakan ditolak.[/yellow]")
            return False
        else:
            display_val = clean[:30] if len(clean) <= 30 else clean[:27] + "..."
            console.print(
                f"[yellow]Pilihan '{escape(display_val)}' tidak valid. "
                "Ketik 'y' untuk menyetujui atau 'n' untuk menolak.[/yellow]"
            )


def _confirm(message):
    """Backward-compatible confirmation helper."""
    return confirm_action(prompt=message)


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


def list_files(path=".", max_depth=None, **kwargs):
    if path is None or path == ".":
        for k in ("dir", "directory", "folder"):
            if k in kwargs and kwargs[k]:
                path = kwargs[k]
                break
    path = path or "."

    if max_depth is None:
        for k in ("depth", "maxdepth"):
            if k in kwargs and kwargs[k] is not None:
                try:
                    max_depth = int(kwargs[k])
                except (ValueError, TypeError):
                    pass
                break

    base = safe_path(path)
    if base.is_file():
        return str(base.relative_to(ROOT))

    cache_key = (str(base), max_depth)
    if cache_key in _DIR_CACHE:
        global _CACHE_HITS
        _CACHE_HITS += 1
        return _DIR_CACHE[cache_key]

    global _CACHE_MISSES
    _CACHE_MISSES += 1

    out = []
    base_parts_len = len(base.parts)
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED)
        if max_depth is not None:
            depth = len(Path(dirpath).parts) - base_parts_len
            if depth >= max_depth:
                dirnames.clear()

        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(ROOT)
            out.append(rel.as_posix())
            if len(out) >= MAX_FILES:
                result = "\n".join(out) + f"\n... (dipotong di {MAX_FILES} file)"
                _DIR_CACHE[cache_key] = result
                return result

    result = "\n".join(out) or "(empty)"
    _DIR_CACHE[cache_key] = result
    return result


def read_file(path=None, start_line=None, end_line=None, **kwargs):
    global _CACHE_HITS, _CACHE_MISSES, _READ_COUNT, _REPEATED_READ_COUNT, _OVERLAPPING_READ_COUNT

    if path is None:
        for k in ("file_path", "filename", "filepath", "target"):
            if k in kwargs and kwargs[k]:
                path = kwargs[k]
                break

    if path is None:
        raise ValueError("Parameter 'path' wajib diisi.")

    if start_line is None:
        for k in ("line_start", "from_line", "start"):
            if k in kwargs and kwargs[k] is not None:
                try:
                    start_line = int(kwargs[k])
                except (ValueError, TypeError):
                    pass
                break
    if end_line is None:
        for k in ("line_end", "to_line", "end"):
            if k in kwargs and kwargs[k] is not None:
                try:
                    end_line = int(kwargs[k])
                except (ValueError, TypeError):
                    pass
                break

    p = safe_path(path)
    if p.is_dir():
        raise IsADirectoryError(f"Path adalah direktori: {path}. Gunakan list_files untuk melihat isi direktori.")
    if not p.is_file():
        raise FileNotFoundError(f"File tidak ditemukan: {path}")
    if _is_sensitive(p):
        raise PermissionError(f"{p.name} berisi data sensitif, tidak boleh dibaca agent.")

    _READ_COUNT += 1

    # Track repeated reads and overlapping ranges within the same task
    read_key = (str(p), start_line, end_line)
    is_repeat = (read_key in _READS_THIS_TASK)
    if is_repeat:
        _REPEATED_READ_COUNT += 1
    else:
        # Determine effective line range for overlap checking
        s_eff = max(1, int(start_line or 1))
        e_eff = int(end_line or 999999)
        p_key = str(p)
        for prev_s, prev_e in _FILE_RANGES_THIS_TASK.get(p_key, []):
            if max(s_eff, prev_s) <= min(e_eff, prev_e):
                _OVERLAPPING_READ_COUNT += 1
                break
        _FILE_RANGES_THIS_TASK.setdefault(p_key, []).append((s_eff, e_eff))

    _READS_THIS_TASK[read_key] = True

    mtime = p.stat().st_mtime
    cache_key = (str(p), mtime)
    if cache_key in _FILE_CACHE:
        _CACHE_HITS += 1
        text = _FILE_CACHE[cache_key]
    else:
        _CACHE_MISSES += 1
        text = p.read_text(encoding="utf-8", errors="replace")
        _FILE_CACHE[cache_key] = text

    if start_line is not None or end_line is not None:
        lines = text.splitlines()
        total_lines = len(lines)
        s = max(1, int(start_line or 1))
        e = min(total_lines, int(end_line or total_lines))
        if s > total_lines:
            return f"(File {p.relative_to(ROOT)} hanya memiliki {total_lines} baris; start_line={s} melebihi batas)"
        selected = lines[s - 1:e]
        numbered = [f"{i + s}: {line}" for i, line in enumerate(selected)]
        res = "\n".join(numbered)

        # Token efficiency: if identical range was already read in this turn and is large, return reference with preview
        if is_repeat and len(res) > 1200 and not kwargs.get("force") and not kwargs.get("reload"):
            preview_subset = numbered[:10]
            return (
                f"[Konten {p.relative_to(ROOT)} (L{s}–L{e}, total {len(selected)} baris • {len(res)} chars) "
                f"identik dengan pembacaan sebelumnya pada giliran ini; file belum berubah.]\n"
                f"Pratinjau awal:\n" + "\n".join(preview_subset) +
                "\n... (konten lengkap sudah ada di konteks di atas; gunakan start_line/end_line spesifik atau force=True jika butuh seluruhnya)"
            )

        if len(res) > 30000:
            res = res[:30000] + "\n... (dipotong pada 30,000 karakter)"
        return res

    # Full file read
    if is_repeat and len(text) > 1200 and not kwargs.get("force") and not kwargs.get("reload"):
        lines = text.splitlines()
        preview_subset = lines[:10]
        return (
            f"[Konten {p.relative_to(ROOT)} (total {len(lines)} baris • {len(text)} chars) "
            f"identik dengan pembacaan sebelumnya pada giliran ini; file belum berubah.]\n"
            f"Pratinjau awal:\n" + "\n".join(preview_subset) +
            "\n... (konten lengkap sudah ada di konteks di atas; gunakan start_line/end_line atau force=True jika butuh seluruhnya)"
        )

    if len(text) > 30000:
        return text[:30000] + "\n... (dipotong pada 30,000 karakter. Gunakan read_file dengan parameter start_line dan end_line untuk membaca bagian spesifik)"
    return text


def write_file(path=None, content=None, **kwargs):
    if path is None:
        for k in ("file_path", "filename", "filepath", "dest", "target_file", "file"):
            if k in kwargs and kwargs[k]:
                path = kwargs[k]
                break
    if content is None:
        for k in ("text", "code", "body", "file_content", "data"):
            if k in kwargs and kwargs[k] is not None:
                content = kwargs[k]
                break

    if path is None:
        raise ValueError("Parameter 'path' wajib diisi.")
    if content is None:
        content = ""
    elif not isinstance(content, str):
        content = str(content)

    p = safe_path(path)

    # Security check: never write or overwrite sensitive files
    if _is_sensitive(p):
        raise PermissionError(f"{p.name} berisi data sensitif, tidak boleh ditulis/ditimpa agent.")

    if p.exists():
        if not p.is_file():
            raise IsADirectoryError(f"Path adalah direktori: {path}")
        try:
            old_text = p.read_text(encoding="utf-8", errors="replace")
            # If content is identical, avoid unnecessary rewrite or confirmation
            if old_text == content:
                invalidate_caches(p)
                return f"wrote {p.relative_to(ROOT)} (sudah up-to-date, konten identik)"
            _show_diff(old_text, content, str(p.relative_to(ROOT)))
        except Exception:
            pass
        if not confirm_action(
            prompt=f"Timpa file yang sudah ada: {escape(str(p.relative_to(ROOT)))}?",
            action_title="Konfirmasi Penimpaan File",
        ):
            console.print("[red]Penimpaan file dibatalkan oleh user.[/red]")
            return "Write rejected by user."

    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    invalidate_caches(p)

    # Verification: check file exists on disk and is readable
    if not p.exists() or not p.is_file():
        raise IOError(f"Verifikasi penulisan file gagal: {path} tidak ditemukan setelah ditulis.")

    return f"wrote {p.relative_to(ROOT)}"


def edit_file(path=None, old_str=None, new_str=None, replace_all=False, **kwargs):
    if path is None:
        for k in ("file_path", "filename", "filepath", "target_file"):
            if k in kwargs and kwargs[k]:
                path = kwargs[k]
                break

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

    if path is None:
        raise ValueError("Parameter 'path' wajib diisi.")
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

    # 1. Exact match check
    if old_str in original_text:
        count = original_text.count(old_str)
        if count > 1 and not replace_all:
            return (
                f"Gagal mengedit {p.relative_to(ROOT)}: 'old_str' ditemukan sebanyak {count} kali di dalam file. "
                "Sertakan lebih banyak baris konteks di sekitarnya agar unik."
            )
        new_content = original_text.replace(old_str, new_str, -1 if replace_all else 1)
    else:
        # 2. Line ending normalization (\r\n vs \n)
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

    if not confirm_action(
        prompt=f"Terapkan perubahan pada: {escape(rel_path)}?",
        action_title="Konfirmasi Edit File",
    ):
        console.print("[red]Perubahan file dibatalkan oleh user.[/red]")
        return "Edit rejected by user."

    p.write_text(new_content, encoding="utf-8")
    invalidate_caches(p)
    return f"edited {rel_path}"


def _command_preview(command):
    command = " ".join(command.split())
    return command if len(command) <= 120 else command[:117] + "..."


def run_command(command=None, **kwargs):
    if command is None:
        for k in ("cmd", "shell_command", "script"):
            if k in kwargs and kwargs[k]:
                command = kwargs[k]
                break

    if not command:
        raise ValueError("Parameter 'command' wajib diisi.")

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
    if not confirm_action(
        prompt="Izinkan eksekusi perintah di atas?",
        action_title="Eksekusi Perintah Shell",
    ):
        console.print("[red]Perintah dibatalkan oleh user.[/red]")
        return "Command rejected by user."

    cmd_short = _command_preview(command)
    try:
        with console.status(f"[bold yellow]Menjalankan '{cmd_short}'...[/bold yellow]", spinner="dots"):
            r = subprocess.run(
                command, shell=True, cwd=ROOT, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=120,
            )
            invalidate_caches()
    except subprocess.TimeoutExpired:
        return "Command timed out after 120s."
    except KeyboardInterrupt:
        return "Command execution interrupted by user (Ctrl+C)."

    return f"exit_code={r.returncode}\nSTDOUT:\n{r.stdout[-12000:]}\nSTDERR:\n{r.stderr[-12000:]}"


TOOL_DEFS = [
    {
        "name": "list_files",
        "description": "List project files recursively. Use this to inspect project structure. Provide path to inspect a specific subdirectory.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative directory path to list (default is '.')"},
                "max_depth": {"type": "integer", "description": "Maximum directory depth recursion limit (optional)"}
            },
            "required": []
        }
    },
    {
        "name": "read_file",
        "description": "Read a text file from the current project. Supports reading specific line ranges for large files.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to file"},
                "start_line": {"type": "integer", "description": "Starting line number (1-indexed, optional)"},
                "end_line": {"type": "integer", "description": "Ending line number (inclusive, optional)"}
            },
            "required": ["path"]
        }
    },
    {
        "name": "edit_file",
        "description": "Edit an existing file by replacing a specific block of text (old_str) with new text (new_str). This is fast, token-efficient, and preferred over write_file for modifying existing files. old_str must uniquely match the code in the file.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to the file to edit"},
                "old_str": {"type": "string", "description": "Exact text block to find and replace"},
                "new_str": {"type": "string", "description": "New replacement text to insert"}
            },
            "required": ["path", "old_str", "new_str"]
        }
    },
    {
        "name": "write_file",
        "description": "Create a new text file or completely overwrite an existing file. For modifying existing files, always prefer edit_file instead.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path to file"},
                "content": {"type": "string", "description": "Full file content to write"}
            },
            "required": ["path", "content"]
        }
    },
    {
        "name": "run_command",
        "description": "Run a shell command in the current project. User approval is required.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command line to execute"}
            },
            "required": ["command"]
        }
    },
]

FUNCTIONS = {
    "list_files": list_files,
    "read_file": read_file,
    "edit_file": edit_file,
    "write_file": write_file,
    "run_command": run_command,
}