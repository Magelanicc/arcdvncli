import json
import os
import platform
import time

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .llm import chat
from .tools import TOOL_DEFS, FUNCTIONS

console = Console()

TOOL_ICONS = {
    "list_files": "📂",
    "read_file": "📖",
    "write_file": "✏️",
    "run_command": "💻",
}

SYSTEM = """You are ARCDVNCLI, an AI coding agent running inside the user's project.
You help inspect, modify, debug, and run software projects.
Current working directory is the project root.
Use tools instead of guessing about files.
Before making changes, inspect relevant files when needed.
Keep responses concise and explain what you changed.
Never expose or request secrets unnecessarily.
For destructive or risky operations, ask the user instead of blindly doing them.
"""

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
        if k == "content":
            lines = len(str(v).splitlines())
            parts.append(f"content=<{lines} baris>")
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


def run_agent(user_input, history):
    messages = [{"role":"system","content":_system_prompt()}] + history
    messages.append({"role":"user","content":user_input})

    for step in range(8):
        with console.status(
            f"[bold cyan]Agent sedang berpikir...[/bold cyan] [dim]langkah {step + 1}/8[/dim]",
            spinner="dots",
        ):
            msg = chat(messages, TOOL_DEFS)

        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            content = msg.get("content") or ""
            messages.append({"role":"assistant","content":content})
            history.extend(messages[-2:])
            return content

        messages.append(msg)
        for call in tool_calls:                       
            name = call["function"]["name"]
            fn = FUNCTIONS.get(name)

            t_start = time.time()
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                with console.status(f"[bold yellow]Menjalankan {name}...[/bold yellow]", spinner="dots"):
                    result = fn(**args) if fn else f"Unknown tool: {name}"
                elapsed = time.time() - t_start
                _show_tool_result(name, args, result, success=True, elapsed=elapsed)
            except Exception as e:
                elapsed = time.time() - t_start
                result = f"Tool error: {type(e).__name__}: {e}"
                _show_tool_result(name, {}, result, success=False, elapsed=elapsed)

            messages.append({
                "role":"tool",
                "tool_call_id":call["id"],
                "content":str(result)
            })

    return "Agent loop berhenti setelah batas 8 langkah."
