import json
import os

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .llm import chat
from .tools import TOOL_DEFS, FUNCTIONS

console = Console()

SYSTEM = """You are ARCDVNCLI, an AI coding agent running inside the user's project.
You help inspect, modify, debug, and run software projects.
Current working directory is the project root.
Use tools instead of guessing about files.
Before making changes, inspect relevant files when needed.
Keep responses concise and explain what you changed.
Never expose or request secrets unnecessarily.
For destructive or risky operations, ask the user instead of blindly doing them.
"""


def _tool_title(name, state):
    icons = {
        "running": "⟳",
        "success": "✓",
        "error": "✗",
    }
    return f"{icons.get(state, '•')} {name}"


def _summarize_output(result, limit=180):
    value = str(result or "").strip()
    if not value:
        return "(empty)"

    lines = [line.strip() for line in value.splitlines() if line.strip()]
    compact = " ".join(lines)

    if len(compact) <= limit:
        return compact

    return compact[:limit - 3] + "..."


def _show_tool_result(name, result, success=True):
    details_enabled = os.getenv("ARCDVNCLI_TOOL_DETAILS", "0") == "1"
    state = "success" if success else "error"
    border = "cyan" if success else "red"

    console.print(
        Panel(
            Text(_summarize_output(result), style="white"),
            title=f"[bold]{_tool_title(name, state)}[/bold]",
            subtitle=f"{len(str(result))} chars",
            border_style=border,
        )
    )

    if details_enabled and result:
        console.print(
            Panel(
                str(result),
                title=f"[dim]{name} output details[/dim]",
                border_style="dim",
            )
        )


def run_agent(user_input, history):
    messages = [{"role":"system","content":SYSTEM}] + history
    messages.append({"role":"user","content":user_input})

    for step in range(8):
        with console.status(
            f"[bold cyan]Agent is thinking...[/bold cyan] [dim]step {step + 1}/8[/dim]",
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
            args = json.loads(call["function"].get("arguments") or "{}")
            fn = FUNCTIONS.get(name)

            console.print(
                Panel(
                    f"[dim]Preparing tool execution[/dim]\n[bold]{name}[/bold]",
                    title=f"[bold yellow]⚙ {name}[/bold yellow]",
                    border_style="yellow",
                )
            )

            try:
                result = fn(**args) if fn else f"Unknown tool: {name}"
                _show_tool_result(name, result, success=True)
            except Exception as e:
                result = f"Tool error: {type(e).__name__}: {e}"
                _show_tool_result(name, result, success=False)

            messages.append({
                "role":"tool",
                "tool_call_id":call["id"],
                "content":str(result)
            })

    return "Agent loop berhenti setelah batas 8 langkah."
