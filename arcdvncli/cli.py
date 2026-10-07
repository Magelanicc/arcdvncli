import os

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.history import FileHistory

from .agent import run_agent
from .llm import load_env, save_setting
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

console = Console()

COMMANDS = [
    "/help",
    "/exit",
    "/quit",
    "/clear",
    "/settings",
    "/status",
    "/details",
]


def banner(model=None):
    os.system("cls" if os.name == "nt" else "clear")

    logo = """[bold cyan]    _   ___  ___[/bold cyan][bold magenta] _____   ___  _ [/bold magenta]
[bold cyan]   /_\ | _ \/ __|[/bold cyan][bold magenta]   \ \ / / \| |[/bold magenta]
[bold cyan]  / _ \|   / (__ [/bold cyan][bold magenta]| |) \ V /| .` |[/bold magenta]
[bold cyan] /_/ \_\_|_\\___|[/bold cyan][bold magenta]___/ \_/ |_|\_|[/bold magenta]"""
    
    subtitle = f"Project: {os.getcwd()}"
    if model:
        subtitle += f"  •  Model: {model}"

    console.print(
        Panel(
            logo,
            title="[bold]ARCDVN Code v0.1[/bold]",
            subtitle=subtitle,
            border_style="cyan",
            padding=(0, 2),
        )
    )

    console.print(
        "[dim]Type /help for commands[/dim]  "
        "[dim]•[/dim]  [dim]Tab autocomplete[/dim]  "
        "[dim]•[/dim]  [dim]Ctrl+C interrupt[/dim]  "
        "[dim]•[/dim]  [dim]/details tool output[/dim]\n"
    )


def _prompt_session():
    return PromptSession(
        history=FileHistory(os.path.expanduser("~/.arcdvncli_history")),
        completer=WordCompleter(
            COMMANDS,
            ignore_case=True,
            sentence=True,
        ),
        complete_while_typing=True,
    )


def _print_context_status(model, history):
    details = os.getenv("ARCDVNCLI_TOOL_DETAILS", "0") == "1"
    console.print(
        f"[dim]Model[/dim] [cyan]{model}[/cyan]  "
        f"[dim]│ Context[/dim] [cyan]{len(history)} msgs[/cyan]  "
        f"[dim]│ Tool details[/dim] [cyan]{'ON' if details else 'OFF'}[/cyan]"
    )


def _command_help():
    table = Table(
        title="ARCDVN Commands",
        title_style="bold cyan",
        border_style="cyan",
        show_header=True,
        padding=(0, 2),
    )
    table.add_column("Command", style="yellow", no_wrap=True)
    table.add_column("Description", style="white")

    rows = [
        ("/help", "Show commands"),
        ("/settings", "Atur OpenRouter API key dan model"),
        ("/status", "Show model, project, and context status"),
        ("/details", "Toggle expanded tool output"),
        ("/clear", "Hapus history percakapan dan refresh screen"),
        ("/exit", "Keluar dari ARCDVNCLI"),
        ("/quit", "Alias /exit"),
    ]

    for command, description in rows:
        table.add_row(command, description)

    console.print(table)


def _settings():
    from .llm import load_env, save_setting

    load_env()
    current_key = os.getenv("ARCDVN_API_KEY", "")
    current_model = os.getenv("ARCDVN_MODEL", "google/gemini-2.5-flash")
    masked_key = f"{current_key[:8]}...{current_key[-4:]}" if len(current_key) > 12 else "(not set)"

    console.print(
        Panel(
            f"[dim]API Key[/dim]  {masked_key}\n"
            f"[dim]Model[/dim]     {current_model}\n"
            f"[dim]Provider[/dim]  OpenRouter",
            title="[bold cyan]Settings[/bold cyan]",
            border_style="cyan",
        )
    )

    console.print(
        "[dim]Masukkan API Key baru (kosongkan jika tidak ingin mengubah): [/dim]",
        end="",
    )
    new_key = input().strip()
    if new_key:
        save_setting("ARCDVN_API_KEY", new_key)

    console.print(
        "[dim]Masukkan nama model baru (kosongkan jika tidak ingin mengubah): [/dim]",
        end="",
    )
    new_model = input().strip()
    if new_model:
        save_setting("ARCDVN_MODEL", new_model)

    console.print("[green]Pengaturan disimpan secara global.[/green]\n")
    return new_model or current_model


def main():
    load_env()
    model = os.getenv("ARCDVN_MODEL", "google/gemini-2.5-flash")
    session = _prompt_session()

    banner(model)
    history = []

    while True:
        try:
            _print_context_status(model, history)
            user = session.prompt("[ARCDVN] › ").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Bye.[/dim]")
            break

        if not user:
            continue
        if user in {"/exit", "/quit"}:
            console.print("[dim]Bye.[/dim]")
            break

        if user == "/help":
            _command_help()
            continue

        if user == "/status":
            console.print(
                Panel(
                    f"[dim]Project[/dim]  {os.getcwd()}\n"
                    f"[dim]Model[/dim]    {model}\n"
                    f"[dim]Messages[/dim] {len(history)}\n"
                    f"[dim]Details[/dim]  {'ON' if os.getenv('ARCDVNCLI_TOOL_DETAILS', '0') == '1' else 'OFF'}",
                    title="[bold cyan]Status[/bold cyan]",
                    border_style="cyan",
                )
            )
            continue

        if user == "/details":
            current = os.getenv("ARCDVNCLI_TOOL_DETAILS", "0") == "1"
            os.environ["ARCDVNCLI_TOOL_DETAILS"] = "0" if current else "1"
            console.print(
                f"[cyan]Tool output details {'disabled' if current else 'enabled'}.[/cyan]"
            )
            continue

        if user == "/clear":
            history.clear()
            banner(model)
            console.print("[green]History percakapan telah dihapus.[/green]\n")
            continue

        if user == "/settings":
            model = _settings()
            continue

        console.print(
            Panel(
                user,
                title="[bold cyan]You[/bold cyan]",
                border_style="cyan",
                padding=(0, 1),
            )
        )

        try:
            answer = run_agent(user, history)

            console.print(
                Panel(
                    Markdown(answer),
                    title="[bold magenta]ARCDVN[/bold magenta]",
                    border_style="magenta",
                    padding=(0, 1),
                )
            )
            console.print()

            if len(history) > 15:
                console.print(
                    "\n[bold yellow][!] Peringatan: Konteks percakapan sudah cukup panjang (memori penuh).[/bold yellow]"
                )
                console.print(
                    "[yellow]    Kekuatan penalaran AI mungkin akan melemah. Disarankan menggunakan perintah /clear.[/yellow]"
                )
        except KeyboardInterrupt:
            console.print("\n[bold yellow]Agent dihentikan oleh user.[/bold yellow]")
            continue
        except Exception as e:
            console.print(
                Panel(
                    f"{type(e).__name__}: {e}",
                    title="[bold red]ARCDVN ERROR[/bold red]",
                    border_style="red",
                )
            )


if __name__ == "__main__":
    main()
