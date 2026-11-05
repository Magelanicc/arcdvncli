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

from .llm import chat, LLMError, reset_llm_metrics, get_llm_task_metrics
from .router import detect_intent, TaskIntent
from .tools import (
    TOOL_DEFS, FUNCTIONS, ROOT, console,
    reset_task_metrics, get_task_metrics,
    record_tool_call, record_tool_result_bytes,
)

TOOL_ICONS = {
    "list_files": "📂",
    "read_file": "📖",
    "edit_file": "📝",
    "write_file": "✏️",
    "run_command": "💻",
}

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------
SYSTEM_CODING = """\
You are ARCDVNCLI, an autonomous AI coding agent running inside the user's terminal.
Your primary goal is to **complete the user's request by making real changes to the project filesystem** — not just explain or show code in chat.

## Core Rules

1. **Write files directly to disk; never just output code in chat.** When the user asks you to create, build, or modify any files, you MUST invoke `write_file` or `edit_file`. NEVER display full code blocks in your final response text and expect the user to copy-paste them. Chat messages do NOT create files on disk.

2. **Inspect when needed, then act.** If the task targets a specific file or doesn't require repo-wide context, inspect that specific file directly instead of listing the whole project. Only use `list_files` when the project structure is unknown or relevant.

3. **Create complete, functional implementations.** When building something (e.g., a landing page, website, or CLI), create ALL necessary files — HTML, CSS, JavaScript, and helper modules. Ensure matching filenames, relative paths, and references between files.

4. **Verify your work.** Check tool execution results. After writing files, verify they were created and non-empty. Run syntax checks or tests when appropriate. If a tool fails or is rejected, do not claim success.

5. **Be self-sufficient.** For standard tasks, make sensible design decisions on your own (color palettes, layouts, directory structures). Only ask the user for clarification when genuine ambiguity blocks progress.

6. **Don't overwrite existing code without reason.** In existing projects, preserve code you did not write. Use `edit_file` to modify specific parts rather than replacing entire files.

7. **Avoid repetitive loops.** If you've already inspected a file or directory, don't inspect it again unless changed. If a tool call fails, try a different approach or fix the arguments — do not repeat the exact same call.

8. **Report results honestly.** When finished, summarize what was created or changed on disk. If something failed, report it accurately.

## Tool Usage

- `list_files` — Inspect project structure. Use when needed, not repeatedly.
- `read_file` — Read a file or specific line range when needed.
- `write_file` — Create new files or overwrite existing files. Writes directly to disk.
- `edit_file` — Modify specific parts of existing files using search-and-replace. Preferred for edits.
- `run_command` — Execute shell commands (requires user approval).

Current working directory is the project root.
Never expose or request secrets unnecessarily.
"""

DEFAULT_MAX_STEPS = 30
_MAX_STEPS = DEFAULT_MAX_STEPS
_MAX_STALL = 3  # consecutive identical tool calls before aborting

# Configurable limits for tool-result and context size
TOOL_RESULT_LIMIT = int(os.getenv("ARCDVN_TOOL_RESULT_LIMIT", "8000"))
CONTEXT_RESULT_LIMIT = int(os.getenv("ARCDVN_CONTEXT_LIMIT", "60000"))

# Module-level metrics for observability
_LAST_METRICS = {}


def get_last_metrics():
    """Return a copy of the last task execution metrics."""
    return dict(_LAST_METRICS)


def _system_prompt():
    shell = "cmd.exe" if os.name == "nt" else os.environ.get("SHELL", "sh")
    return (
        SYSTEM_CODING
        + f"Operating system: {platform.system()} {platform.release()}. Shell: {shell}.\n"
        + "Use only commands that are valid for this OS and shell.\n"
    )


def _conversational_prompt(intent):
    if intent == TaskIntent.EXPLANATION:
        return (
            "You are ARCDVNCLI, an expert software engineering assistant running inside the user's terminal.\n"
            "Provide clear, direct, insightful, and well-structured technical explanations in the user's language.\n"
            "Answer directly without inspecting project files or using tools unless explicitly requested."
        )
    return (
        "You are ARCDVNCLI, a fast and helpful AI coding assistant running inside the user's terminal.\n"
        "Respond warmly, clearly, and concisely in the user's language.\n"
        "Keep everyday conversation natural and direct without calling tools or scanning project files."
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


def _truncate_tool_result_for_llm(content, limit=None):
    """
    Truncate excessively large tool outputs before passing to LLM context,
    preserving initial context (headers, command start) and critical tail
    diagnostics (STDERR, tracebacks, final exception messages).
    """
    if limit is None:
        limit = TOOL_RESULT_LIMIT
    s = str(content or "")
    if len(s) <= limit:
        return s

    head_len = int(limit * 0.35)
    tail_len = int(limit * 0.55)
    omitted = len(s) - head_len - tail_len

    head = s[:head_len]
    tail = s[-tail_len:]
    notice = (
        f"\n\n... [dipotong: total {len(s)} karakter ({omitted} karakter bagian tengah disembunyikan). "
        "Gunakan parameter start_line/end_line atau path/perintah spesifik untuk melihat bagian tengah] ...\n\n"
    )
    return head + notice + tail


def _compact_read_file_display(name, args, result, elapsed):
    """Generate a compact one-line terminal display for read_file results."""
    path = args.get("path") or args.get("file_path") or "?"
    start = args.get("start_line")
    end = args.get("end_line")

    result_str = str(result or "")
    line_count = len(result_str.splitlines())
    char_count = len(result_str)

    if start or end:
        range_str = f"L{start or 1}–L{end or '?'}"
    else:
        range_str = f"{line_count} lines"

    console.print(
        f"  [bold cyan]📖 Agent Read File[/bold cyan] : "
        f"[white]{path}[/white] [dim]({range_str} • {char_count} chars • {elapsed:.2f}s)[/dim]"
    )


def _compact_list_files_display(args, result, elapsed):
    """Generate a compact terminal display for list_files results."""
    path = args.get("path") or "."
    result_str = str(result or "")
    file_count = len([l for l in result_str.splitlines() if l.strip()])

    console.print(
        f"  [bold cyan]📂 Agent Listed Files[/bold cyan] : "
        f"[white]{path}[/white] [dim]({file_count} files • {elapsed:.2f}s)[/dim]"
    )


def _get_status_message(name, args):
    """Generate informative, concise progress messages for user visibility."""
    path = args.get("path") or args.get("file_path") or args.get("filename")
    if name == "read_file" and path:
        return f"[bold yellow]📖 Membaca {path}...[/bold yellow]"
    elif name == "write_file" and path:
        return f"[bold yellow]✏️ Menulis {path}...[/bold yellow]"
    elif name == "edit_file" and path:
        return f"[bold yellow]📝 Mengedit {path}...[/bold yellow]"
    elif name == "list_files":
        target = f" di {path}" if path and path != "." else ""
        return f"[bold yellow]📂 Memeriksa struktur file{target}...[/bold yellow]"
    elif name == "run_command":
        cmd = args.get("command", "")
        cmd_short = cmd if len(cmd) <= 30 else cmd[:27] + "..."
        return f"[bold yellow]💻 Menjalankan '{cmd_short}'...[/bold yellow]"
    return f"[bold yellow]Menjalankan {name}...[/bold yellow]"


def _show_tool_result(name, args, result, success=True, elapsed=0.0):
    """Display tool results in terminal — compact for reads, paneled for writes/edits/commands."""
    details_enabled = os.getenv("ARCDVNCLI_TOOL_DETAILS", "0") == "1"

    # Compact display for read-only operations (clean terminal UI §7)
    if name == "read_file" and success and not details_enabled:
        _compact_read_file_display(name, args, result, elapsed)
        return
    if name == "list_files" and success and not details_enabled:
        _compact_list_files_display(args, result, elapsed)
        return

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
    """Check if model outputs code blocks in chat text while no files have been written to disk yet."""
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


def _estimate_context_chars(messages):
    """Estimate the total character count of the message history."""
    total = 0
    for msg in messages:
        content = msg.get("content") or ""
        total += len(content)
        # Count tool call arguments
        tool_calls = msg.get("tool_calls") or []
        for tc in tool_calls:
            func = tc.get("function") or {}
            total += len(func.get("arguments", ""))
    return total


def _compact_history_if_needed(messages, limit=None):
    """
    Compact old tool results in message history when context grows too large.
    Preserves system, recent user/assistant messages, and recent tool results.
    Replaces old tool results with compact summaries.
    """
    if limit is None:
        limit = CONTEXT_RESULT_LIMIT

    total = _estimate_context_chars(messages)
    if total <= limit:
        return

    # Find tool-result messages (not the last 4) and summarize them
    tool_indices = [
        i for i, msg in enumerate(messages)
        if msg.get("role") == "tool" and i < len(messages) - 4
    ]

    for idx in tool_indices:
        content = messages[idx].get("content", "")
        if len(content) > 500:
            # Replace with compact summary
            messages[idx] = {
                **messages[idx],
                "content": content[:300] + f"\n... [kompaksi: {len(content)} chars asli, dipotong untuk efisiensi konteks]"
            }


def _handle_direct_conversation(user_input, history, intent, t_start):
    """
    Fast direct conversational response without filesystem inspection or tool overhead.
    Handles conversation, greetings, and conceptual Q&A with 1 single LLM call.
    """
    global _LAST_METRICS
    messages = [{"role": "system", "content": _conversational_prompt(intent)}] + history
    messages.append({"role": "user", "content": user_input})

    llm_calls = 0
    empty_responses = 0

    while empty_responses <= 1:
        try:
            with console.status(
                "[bold cyan]Agent sedang berpikir...[/bold cyan]",
                spinner="dots",
            ):
                llm_calls += 1
                msg = chat(messages, tools=None)
        except LLMError as e:
            if e.retryable:
                return f"⚠️ LLM sementara tidak tersedia: {e}"
            return f"❌ LLM error: {e}"

        content = msg.get("content") or ""
        # If response was empty, nudge once
        if not content.strip():
            empty_responses += 1
            if empty_responses <= 1:
                messages.append({"role": "assistant", "content": ""})
                messages.append({
                    "role": "user",
                    "content": "Respons kosong. Tolong berikan jawaban Anda.",
                })
                continue
            return "Model memberikan respons kosong."

        # Valid text response
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": content})

        elapsed = time.time() - t_start
        llm_data = get_llm_task_metrics()
        _LAST_METRICS = {
            "intent": intent,
            "duration": round(elapsed, 2),
            "llm_calls": llm_calls,
            "http_attempts": llm_data.get("http_attempts", llm_calls),
            "internal_retries": llm_data.get("internal_retries", 0),
            "tokens": llm_data,
            "tool_calls": 0,
            "steps": 1,
            "tool_duration": 0.0,
            "tool_metrics": get_task_metrics(),
            "retry_count": 0,
            "failure_categories": {},
        }

        # If diagnostics explicitly requested via env var
        if os.getenv("ARCDVN_DIAGNOSTICS") == "1":
            return content + f"\n\n[dim]⚡ {intent} • {elapsed:.2f}s • {llm_calls} LLM call[/dim]"
        return content

    return "Model memberikan respons kosong."


def run_agent(user_input, history):
    global _LAST_METRICS
    t_start = time.time()

    # Reset per-task metrics
    reset_task_metrics()
    reset_llm_metrics()

    # Phase 2: Intent-Aware Lightweight Routing
    intent = detect_intent(user_input)

    # Route pure conversation and general conceptual Q&A to direct path
    if intent in (TaskIntent.CONVERSATION, TaskIntent.EXPLANATION):
        return _handle_direct_conversation(user_input, history, intent, t_start)

    # Task-adaptive step limits
    env_steps = os.getenv("ARCDVN_MAX_STEPS")
    if env_steps:
        max_steps = int(env_steps)
    elif intent in (TaskIntent.INSPECTION, TaskIntent.COMMAND):
        max_steps = 10
    else:
        max_steps = DEFAULT_MAX_STEPS

    messages = [{"role": "system", "content": _system_prompt()}] + history
    messages.append({"role": "user", "content": user_input})

    touched_files = {}          # path -> "ok" | "failed" | "rejected"
    recent_signatures = []      # list of tool call signatures for stall detection
    all_call_signatures = []    # flat list of individual call signatures for cycle detection
    empty_responses = 0         # consecutive empty responses
    code_nudges = 0             # consecutive code-in-terminal steering nudges
    llm_calls = 0
    tool_calls_count = 0
    tool_total_duration = 0.0
    retry_count = 0
    failure_categories = {}

    for step in range(max_steps):
        # Compact history if context is growing too large (§4)
        _compact_history_if_needed(messages)

        # --- Call LLM ---
        try:
            with console.status(
                f"[bold cyan]Agent sedang berpikir...[/bold cyan] [dim]langkah {step + 1}/{max_steps}[/dim]",
                spinner="dots",
            ):
                llm_calls += 1
                msg = chat(messages, TOOL_DEFS)
        except LLMError as e:
            summary = _build_file_summary(touched_files)
            category = "retryable" if e.retryable else "permanent"
            failure_categories[category] = failure_categories.get(category, 0) + 1
            retry_count += 1
            elapsed = time.time() - t_start
            llm_data = get_llm_task_metrics()
            _LAST_METRICS = {
                "intent": intent,
                "duration": round(elapsed, 2),
                "llm_calls": llm_calls,
                "http_attempts": llm_data.get("http_attempts", llm_calls),
                "internal_retries": llm_data.get("internal_retries", 0),
                "tokens": llm_data,
                "tool_calls": tool_calls_count,
                "steps": step + 1,
                "tool_duration": round(tool_total_duration, 2),
                "tool_metrics": get_task_metrics(),
                "retry_count": retry_count,
                "failure_categories": dict(failure_categories),
            }
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

            elapsed = time.time() - t_start
            llm_data = get_llm_task_metrics()
            _LAST_METRICS = {
                "intent": intent,
                "duration": round(elapsed, 2),
                "llm_calls": llm_calls,
                "http_attempts": llm_data.get("http_attempts", llm_calls),
                "internal_retries": llm_data.get("internal_retries", 0),
                "tokens": llm_data,
                "tool_calls": tool_calls_count,
                "steps": step + 1,
                "tool_duration": round(tool_total_duration, 2),
                "tool_metrics": get_task_metrics(),
                "retry_count": retry_count,
                "failure_categories": dict(failure_categories),
            }

            if os.getenv("ARCDVN_DIAGNOSTICS") == "1":
                final += f"\n\n[dim]⚡ {intent} • {elapsed:.2f}s • {llm_calls} LLM call(s) • {tool_calls_count} tool(s)[/dim]"

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
            tool_calls_count += 1
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

            # Track per-tool call counts
            record_tool_call(name)

            fn = FUNCTIONS.get(name)
            raw_args = func_dict.get("arguments") or "{}"
            t_tool_start = time.time()

            try:
                if isinstance(raw_args, dict):
                    args = raw_args
                elif isinstance(raw_args, str):
                    clean_raw = raw_args.strip()
                    if clean_raw.startswith("```json") and clean_raw.endswith("```"):
                        clean_raw = clean_raw[7:-3].strip()
                    elif clean_raw.startswith("```") and clean_raw.endswith("```"):
                        clean_raw = clean_raw[3:-3].strip()
                    args = json.loads(clean_raw) if clean_raw else {}
                else:
                    args = {}
            except (json.JSONDecodeError, ValueError):
                elapsed = time.time() - t_tool_start
                tool_total_duration += elapsed
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
                elapsed = time.time() - t_tool_start
                tool_total_duration += elapsed
                result = f"Unknown tool: '{name}'. Available tools: {', '.join(FUNCTIONS.keys())}"
                _show_tool_result(name, args, result, success=False, elapsed=elapsed)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result,
                })
                continue

            try:
                if name == "run_command":
                    # run_command handles its own confirmation UI and execution spinner
                    result = fn(**args)
                else:
                    status_msg = _get_status_message(name, args)
                    with console.status(status_msg, spinner="dots"):
                        result = fn(**args)
                elapsed = time.time() - t_tool_start
                tool_total_duration += elapsed
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
                elif name == "run_command":
                    if isinstance(result, str):
                        if "rejected" in result.lower() or "interrupted" in result.lower() or "timed out" in result.lower():
                            success = False
                        elif result.startswith("exit_code=") and not result.startswith("exit_code=0"):
                            success = False

                _show_tool_result(name, args, result, success=success, elapsed=elapsed)
            except Exception as e:
                elapsed = time.time() - t_tool_start
                tool_total_duration += elapsed
                result = f"Tool error: {type(e).__name__}: {e}"
                _show_tool_result(name, args, result, success=False, elapsed=elapsed)

                target_path = args.get("path") or args.get("file_path") or args.get("filename")
                if name in ("write_file", "edit_file") and target_path:
                    touched_files[target_path] = "failed"

                category = type(e).__name__
                failure_categories[category] = failure_categories.get(category, 0) + 1

            # Context size protection: truncate oversized tool results for LLM
            content_for_llm = _truncate_tool_result_for_llm(result)
            record_tool_result_bytes(len(content_for_llm))
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": content_for_llm,
            })

    # --- Max steps reached ---
    elapsed = time.time() - t_start
    llm_data = get_llm_task_metrics()
    _LAST_METRICS = {
        "intent": intent,
        "duration": round(elapsed, 2),
        "llm_calls": llm_calls,
        "http_attempts": llm_data.get("http_attempts", llm_calls),
        "internal_retries": llm_data.get("internal_retries", 0),
        "tokens": llm_data,
        "tool_calls": tool_calls_count,
        "steps": max_steps,
        "tool_duration": round(tool_total_duration, 2),
        "tool_metrics": get_task_metrics(),
        "retry_count": retry_count,
        "failure_categories": dict(failure_categories),
    }

    summary = _build_file_summary(touched_files)
    return (
        f"⚠️ Agent berhenti setelah batas {max_steps} langkah.{summary}\n\n"
        "Jika pekerjaan belum selesai, Anda dapat melanjutkan dengan mengirimkan instruksi lanjutan."
    )
