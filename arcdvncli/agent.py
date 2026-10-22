import json
import os
import platform
import re
import time
from pathlib import Path

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .llm import chat, LLMError
from .tools import TOOL_DEFS, FUNCTIONS, ROOT

console = Console()

TOOL_ICONS = {
    "list_files": "📂",
    "read_file": "📖",
    "edit_file": "📝",
    "write_file": "✏️",
    "run_command": "💻",
}

# ---------------------------------------------------------------------------
# System prompt — action-oriented coding agent
# ---------------------------------------------------------------------------
SYSTEM = """\
You are ARCDVNCLI, an autonomous AI coding agent running inside the user's terminal.
Your primary goal is to **complete the user's request by making real changes to the project filesystem** — not just explain or show code in chat.

## Core Rules

1. **Write files directly to disk; never just output code in chat.** When the user asks you to create, build, or modify any files, you MUST invoke `write_file` or `edit_file`. NEVER display full code blocks in your final response text and expect the user to copy-paste them. Chat messages do NOT create files on disk.

2. **Inspect first, then act.** Use `list_files` once at the start to understand the project structure. If the project is empty, create all required files using `write_file`. If the project already has code, use `read_file` to inspect relevant files before making changes.

3. **Create complete, functional implementations.** When building something (e.g., a landing page, website, or CLI), create ALL necessary files — HTML, CSS, JavaScript, and helper modules. Ensure matching filenames, relative paths, and references between files.

4. **Verify your work.** Check tool execution results. After writing files, verify they were created and non-empty. If a tool fails or is rejected, do not claim success.

5. **Be self-sufficient.** For standard tasks, make sensible design decisions on your own (color palettes, layouts, directory structures). Only ask the user for clarification when genuine ambiguity blocks progress.

6. **Don't overwrite existing code without reason.** In existing projects, preserve code you did not write. Use `edit_file` to modify specific parts rather than replacing entire files.

7. **Avoid repetitive loops.** If you've already inspected a file or directory, don't inspect it again unless changed. If a tool call fails, try a different approach or fix the arguments — do not repeat the exact same call.

8. **Report results honestly.** When finished, summarize what was created or changed on disk. If something failed, report it accurately.

## Tool Usage

- `list_files` — Inspect project structure. Use once at the beginning, not repeatedly.
- `read_file` — Read a file when needed for decision-making.
- `write_file` — Create new files or overwrite existing files. Writes directly to disk.
- `edit_file` — Modify specific parts of existing files using search-and-replace. Preferred for edits.
- `run_command` — Execute shell commands (requires user approval).

Current working directory is the project root.
Never expose or request secrets unnecessarily.
"""

DEFAULT_MAX_STEPS = 30
_MAX_STEPS = DEFAULT_MAX_STEPS
_MAX_STALL = 3  # consecutive identical tool calls before aborting


def _system_prompt():
    shell = "cmd.exe" if os.name == "nt" else os.environ.get("SHELL", "sh")
    return (
        SYSTEM
        + f"Operating system: {platform.system()} {platform.release()}. Shell: {shell}.\n"
        + "Use only commands that are valid for this OS and shell.\n"
    )


def _format_args_preview(args):
    if not args or not isinstance(args, dict):
        return ""
    parts = []
    for k, v in args.items():
        if k in ("content", "old_str", "new_str", "old_text", "new_text", "text", "code"):
            val_lines = str(v).splitlines()
            if len(val_lines) > 1:
                parts.append(f"{k}=<{len(val_lines)} baris>")
            else:
                display_val = str(v) if len(str(v)) <= 45 else str(v)[:42] + "..."
                parts.append(f'{k}="{display_val}"')
        elif isinstance(v, str):
            display_val = v if len(v) <= 45 else v[:42] + "..."
            parts.append(f'{k}="{display_val}"')
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts)


def _summarize_output(result, limit=180):
    value = str(result or "").strip()
    if not value:
        return "(empty)"

    lines = [line.strip() for line in value.splitlines() if line.strip()]
    compact = " ".join(lines)

    if len(compact) <= limit:
        return compact

    return compact[:limit - 3] + "..."


def _show_tool_result(name, args, result, success=True, elapsed=0.0):
    details_enabled = os.getenv("ARCDVNCLI_TOOL_DETAILS", "0") == "1"
    icon = TOOL_ICONS.get(name, "⚙")
    args_str = _format_args_preview(args)
    
    title = f"[bold cyan]{icon} {name}[/bold cyan]"
    if args_str:
        title += f" [dim]({args_str})[/dim]"

    status_badge = "[bold green]✓ Selesai[/bold green]" if success else "[bold red]✗ Gagal[/bold red]"
    subtitle = f"{status_badge} [dim]• {elapsed:.2f}s • {len(str(result))} chars[/dim]"
    border = "cyan" if success else "red"

    console.print(
        Panel(
            Text(_summarize_output(result), style="white"),
            title=title,
            subtitle=subtitle,
            border_style=border,
            box=box.ROUNDED,
            padding=(0, 1),
        )
    )

    if details_enabled and result:
        console.print(
            Panel(
                str(result),
                title=f"[dim]{name} output details[/dim]",
                border_style="bright_black",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )


def _tool_call_signature(call):
    """Return a hashable signature for detecting repeated identical tool calls."""
    func = call.get("function") or {}
    name = func.get("name", "") if isinstance(func, dict) else ""
    args_raw = func.get("arguments", "") if isinstance(func, dict) else ""
    return (name, str(args_raw))


def _build_file_summary(touched_files, verify_disk=True):
    """Build a human-readable summary of files modified during the agent loop."""
    if not touched_files:
        return ""
    lines = ["\n\n📁 **File yang dibuat/diubah:**"]
    for path, status in touched_files.items():
        verified_status = status
        if verify_disk and status == "ok":
            try:
                p = (ROOT / path).resolve()
                if not p.exists() or not p.is_file():
                    verified_status = "gagal (tidak ditemukan di disk)"
            except Exception:
                pass
        icon = "✅" if "ok" in verified_status else "❌"
        lines.append(f"  {icon} {path} ({verified_status})")
    return "\n".join(lines)


def _contains_unwritten_code(content, touched_files):
    """
    Check if the model outputs code blocks in chat text while no files have been written to disk yet.
    """
    has_ok_files = any(status == "ok" for status in touched_files.values())
    if has_ok_files:
        return False

    code_blocks = re.findall(r'```([a-zA-Z0-9_\-\+]*)\n([\s\S]*?)```', content)
    if not code_blocks:
        return False

    for lang, block in code_blocks:
        lang_lower = lang.lower().strip()
        if lang_lower in (
            "html", "css", "js", "javascript", "python", "py", "ts",
            "typescript", "json", "sh", "bash", "c", "cpp", "go", "rs", "php", "sql"
        ) or len(block.strip()) >= 40:
            return True
    return False


def _detect_cycle(signatures_history):
    """
    Detect repetitive tool calling patterns:
    1. Same signature repeated 3 times in a row.
    2. Alternating 2-cycle repeated 3 times (A -> B -> A -> B -> A -> B).
    """
    if len(signatures_history) >= 3:
        if signatures_history[-1] == signatures_history[-2] == signatures_history[-3]:
            return True, "tool call yang sama diulang 3 kali berturut-turut"

    if len(signatures_history) >= 6:
        s = signatures_history
        if s[-1] == s[-3] == s[-5] and s[-2] == s[-4] == s[-6] and s[-1] != s[-2]:
            return True, "loop bolak-balik antara dua tool call terdeteksi"

    return False, ""


def run_agent(user_input, history):
    messages = [{"role": "system", "content": _system_prompt()}] + history
    messages.append({"role": "user", "content": user_input})

    touched_files = {}          # path -> "ok" | "failed" | "rejected"
    recent_signatures = []      # list of tool call signatures for stall detection
    all_call_signatures = []    # flat list of individual call signatures for cycle detection
    empty_responses = 0         # consecutive empty responses
    code_nudges = 0             # consecutive code-in-terminal steering nudges

    max_steps = int(os.getenv("ARCDVN_MAX_STEPS", str(DEFAULT_MAX_STEPS)))

    for step in range(max_steps):
        # --- Call LLM ---
        try:
            with console.status(
                f"[bold cyan]Agent sedang berpikir...[/bold cyan] [dim]langkah {step + 1}/{max_steps}[/dim]",
                spinner="dots",
            ):
                msg = chat(messages, TOOL_DEFS)
        except LLMError as e:
            summary = _build_file_summary(touched_files)
            if e.retryable:
                error_text = (
                    f"⚠️ LLM sementara tidak tersedia: {e}\n\n"
                    f"Pekerjaan yang sudah selesai tetap tersimpan.{summary}"
                )
            else:
                error_text = f"❌ LLM error: {e}{summary}"
            return error_text

        tool_calls = msg.get("tool_calls") or []
        content = msg.get("content") or ""

        # --- No tool calls: model responded with text ---
        if not tool_calls:
            # Check for empty or whitespace-only response
            if not content or not content.strip():
                empty_responses += 1
                if empty_responses <= 1:
                    messages.append({"role": "assistant", "content": ""})
                    messages.append({
                        "role": "user",
                        "content": (
                            "Respons kosong. Jika tugas belum selesai, "
                            "lanjutkan dengan tool call yang diperlukan. "
                            "Jika sudah selesai, berikan ringkasan hasilnya."
                        ),
                    })
                    continue
                summary = _build_file_summary(touched_files)
                return f"Model memberikan respons kosong.{summary}"

            # Check for "code output in terminal only" behavior
            if _contains_unwritten_code(content, touched_files) and code_nudges < 2:
                code_nudges += 1
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": (
                        "PERINGATAN: Anda menampilkan kode di chat tetapi belum menulisnya ke disk. "
                        "Anda WAJIB memanggil tool 'write_file' untuk membuat file tersebut secara nyata di disk. "
                        "Jangan minta user untuk menyalin kode. Panggil 'write_file' sekarang."
                    ),
                })
                continue

            # Valid text response — task completed
            messages.append({"role": "assistant", "content": content})
            summary = _build_file_summary(touched_files)
            final = content + summary if summary else content

            # Update history with condensed representation
            history.append({"role": "user", "content": user_input})
            if touched_files:
                history.append({
                    "role": "assistant",
                    "content": content + f"\n\n[File yang diproses: {', '.join(touched_files.keys())}]"
                })
            else:
                history.append({"role": "assistant", "content": content})

            return final

        # --- Has tool calls: process them ---
        empty_responses = 0  # reset on tool calls

        # Stall & cycle detection
        current_sigs = [_tool_call_signature(c) for c in tool_calls]
        for sig in current_sigs:
            all_call_signatures.append(sig)

        is_cycle, cycle_reason = _detect_cycle(all_call_signatures)
        if is_cycle:
            summary = _build_file_summary(touched_files)
            return (
                f"⚠️ Agent mendeteksi loop: {cycle_reason}. "
                f"Berhenti untuk mencegah pemborosan.{summary}"
            )

        if recent_signatures and current_sigs == recent_signatures[-1]:
            recent_signatures.append(current_sigs)
            if len(recent_signatures) >= _MAX_STALL:
                summary = _build_file_summary(touched_files)
                return (
                    f"⚠️ Agent mendeteksi loop: tool call yang sama diulang "
                    f"{_MAX_STALL} kali berturut-turut. Berhenti untuk mencegah pemborosan.{summary}"
                )
        else:
            recent_signatures = [current_sigs]

        # Append assistant message containing tool calls
        messages.append(msg)

        for idx, call in enumerate(tool_calls):
            call_id = call.get("id") or f"call_{idx}_{int(time.time() * 1000)}"
            func_dict = call.get("function") if isinstance(call, dict) else {}
            name = func_dict.get("name") if isinstance(func_dict, dict) else None

            if not name:
                result = "Tool error: nama tool call tidak valid atau kosong."
                _show_tool_result("unknown", {}, result, success=False)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result,
                })
                continue

            fn = FUNCTIONS.get(name)
            raw_args = func_dict.get("arguments") or "{}"
            t_start = time.time()

            try:
                if isinstance(raw_args, dict):
                    args = raw_args
                else:
                    args = json.loads(raw_args)
            except (json.JSONDecodeError, ValueError):
                elapsed = time.time() - t_start
                result = (
                    f"Tool error: argumen JSON tidak valid. "
                    f"Raw: {str(raw_args)[:200]}"
                )
                _show_tool_result(name, {}, result, success=False, elapsed=elapsed)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result,
                })
                continue

            if not fn:
                elapsed = time.time() - t_start
                result = f"Unknown tool: '{name}'. Available tools: {', '.join(FUNCTIONS.keys())}"
                _show_tool_result(name, args, result, success=False, elapsed=elapsed)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result,
                })
                continue

            try:
                with console.status(f"[bold yellow]Menjalankan {name}...[/bold yellow]", spinner="dots"):
                    result = fn(**args)
                elapsed = time.time() - t_start
                success = True

                # Determine file target path if present
                target_path = args.get("path") or args.get("file_path") or args.get("filename")

                if name == "write_file" and target_path:
                    if isinstance(result, str) and result.startswith("wrote"):
                        touched_files[target_path] = "ok"
                    elif isinstance(result, str) and "rejected" in result.lower():
                        touched_files[target_path] = "rejected"
                        success = False
                    else:
                        touched_files[target_path] = "failed"
                        success = False
                elif name == "edit_file" and target_path:
                    if isinstance(result, str) and result.startswith("edited"):
                        touched_files[target_path] = "ok"
                    elif isinstance(result, str) and "rejected" in result.lower():
                        touched_files[target_path] = "rejected"
                        success = False
                    else:
                        touched_files[target_path] = "failed"
                        success = False

                _show_tool_result(name, args, result, success=success, elapsed=elapsed)
            except Exception as e:
                elapsed = time.time() - t_start
                result = f"Tool error: {type(e).__name__}: {e}"
                _show_tool_result(name, args, result, success=False, elapsed=elapsed)

                target_path = args.get("path") or args.get("file_path") or args.get("filename")
                if name in ("write_file", "edit_file") and target_path:
                    touched_files[target_path] = "failed"

            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": str(result),
            })

    # --- Max steps reached ---
    summary = _build_file_summary(touched_files)
    return (
        f"⚠️ Agent berhenti setelah batas {max_steps} langkah.{summary}\n\n"
        "Jika pekerjaan belum selesai, Anda dapat melanjutkan dengan mengirimkan instruksi lanjutan."
    )
