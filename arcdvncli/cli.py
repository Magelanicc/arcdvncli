import getpass
import os
import subprocess
import sys

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.styles import Style
from rich import box
from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from . import __version__
from .agent import run_agent
from .llm import load_env, save_setting
from .updater import check_for_update

console = Console()

COMMAND_MAP = {
    "/help": "Tampilkan panduan dan daftar perintah",
    "/settings": "Atur API Key OpenRouter dan Model",
    "/status": "Lihat status proyek, git, context window, dan model",
    "/diff": "Tinjau git diff perubahan kode di proyek saat ini",
    "/details": "Aktifkan/nonaktifkan output detail dari tool",
    "/clear": "Hapus riwayat percakapan dan segarkan layar",
    "/uninstall": "Copot ARCDVN Code dan bersihkan seluruh data tanpa sisa",
    "/exit": "Keluar dari ARCDVN Code",
    "/quit": "Keluar dari ARCDVN Code (alias /exit)",
}
COMMANDS = list(COMMAND_MAP.keys())


def _get_git_branch():
    try:
        r = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=1,
            cwd=os.getcwd(),
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


def banner(model=None):
    os.system("cls" if os.name == "nt" else "clear")

    model_name = model or os.getenv("ARCDVN_MODEL", "google/gemini-2.5-flash")
    cwd_name = os.path.basename(os.getcwd()) or os.getcwd()
    git_branch = _get_git_branch()
    git_badge = f"  •  [bold green]🌿 {git_branch}[/bold green]" if git_branch else ""

    logo = (
        r"[bold cyan]    ___    ____   ______ ____  _    __ _   __[/bold cyan]" "\n"
        r"[bold cyan]   /   |  / __ \ / ____// __ \| |  / // | / /[/bold cyan]" "\n"
        r"[bold magenta]  / /| | / /_/ // /    / / / /| | / //  |/ / [/bold magenta]" "\n"
        r"[bold magenta] / ___ |/ _, _// /___ / /_/ / | |/ // /|  /  [/bold magenta]" "\n"
        r"[bold blue]/_/  |_/_/ |_| \____//_____/  |___//_/ |_/   [/bold blue]"
    )

    info_line = (
        f"[dim]Project:[/dim] [bold white]{cwd_name}[/bold white]{git_badge}  •  "
        f"[dim]Model:[/dim] [bold cyan]{model_name}[/bold cyan]  •  "
        f"[dim]v{__version__}[/dim]"
    )

    console.print(
        Panel(
            f"{logo}\n\n{info_line}",
            title="[bold cyan]◆ ARCDVN CODE ◆[/bold cyan]",
            subtitle="[dim]Autonomous Terminal AI Agent[/dim]",
            border_style="cyan",
            box=box.ROUNDED,
            padding=(0, 2),
        )
    )

    console.print(
        "[dim]Tips:[/dim] [cyan]/help[/cyan] [dim]perintah[/dim] • "
        "[cyan]/settings[/cyan] [dim]atur openrouter[/dim] • "
        "[cyan]Tab[/cyan] [dim]autocomplete[/dim] • "
        "[cyan]/diff[/cyan] [dim]cek perubahan[/dim] • "
        "[cyan]Ctrl+C[/cyan] [dim]batal[/dim]\n"
    )


class ArcCommandCompleter(Completer):
    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if text.startswith("/"):
            for cmd, desc in COMMAND_MAP.items():
                if cmd.lower().startswith(text.lower()):
                    yield Completion(
                        cmd,
                        start_position=-len(text),
                        display=cmd,
                        display_meta=desc,
                    )


def _prompt_session():
    style = Style.from_dict({
        "prompt": "#00d7d7 bold",
        "bottom-toolbar": "bg:#1e1e2e #cdd6f4",
        "bottom-toolbar.model": "#89b4fa bold",
        "bottom-toolbar.branch": "#a6e3a1",
        "bottom-toolbar.dim": "#6c7086",
    })

    return PromptSession(
        history=FileHistory(os.path.expanduser("~/.arcdvncli_history")),
        completer=ArcCommandCompleter(),
        complete_while_typing=True,
        style=style,
    )


def _print_context_status(model, history):
    details = os.getenv("ARCDVNCLI_TOOL_DETAILS", "0") == "1"
    git_branch = _get_git_branch()
    branch_str = f" [dim]│ Branch[/dim] [green]{git_branch}[/green]" if git_branch else ""
    console.print(
        f"[dim]Model[/dim] [cyan]{model}[/cyan]  "
        f"[dim]│ Context[/dim] [cyan]{len(history)} msgs[/cyan]{branch_str}  "
        f"[dim]│ Details[/dim] [cyan]{'ON' if details else 'OFF'}[/cyan]"
    )


def _command_help():
    table = Table(
        title="Daftar Perintah ARCDVN Code",
        title_style="bold cyan",
        border_style="cyan",
        box=box.ROUNDED,
        show_header=True,
        padding=(0, 2),
    )
    table.add_column("Perintah", style="bold yellow", no_wrap=True)
    table.add_column("Deskripsi", style="white")

    for command, description in COMMAND_MAP.items():
        if command != "/quit":
            table.add_row(command, description)

    console.print(table)


def _settings():
    load_env()
    current_key = os.getenv("ARCDVN_API_KEY", "")
    current_model = os.getenv("ARCDVN_MODEL", "google/gemini-2.5-flash")
    masked_key = f"{current_key[:8]}...{current_key[-4:]}" if len(current_key) > 12 else "(belum disetel)"

    console.print(
        Panel(
            f"[dim]Provider:[/dim]  [bold]OpenRouter[/bold] (openrouter.ai/models)\n"
            f"[dim]API Key :[/dim]  [bold yellow]{masked_key}[/bold yellow]\n"
            f"[dim]Model   :[/dim]  [bold cyan]{current_model}[/bold cyan]",
            title="[bold cyan]Pengaturan ARCDVN (OpenRouter)[/bold cyan]",
            border_style="cyan",
            box=box.ROUNDED,
            padding=(0, 2),
        )
    )

    console.print("[dim]Masukkan API Key OpenRouter baru (kosongkan jika tidak ingin mengubah): [/dim]", end="")
    try:
        new_key = getpass.getpass("").strip()
    except Exception:
        new_key = input().strip()

    if new_key:
        save_setting("ARCDVN_API_KEY", new_key)
        console.print("[green]✓ API Key OpenRouter berhasil disimpan.[/green]")

    console.print(
        "[dim]Masukkan Model OpenRouter baru (kosongkan jika tidak ingin mengubah)\n"
        "Contoh: google/gemini-2.5-flash, deepseek/deepseek-chat, meta-llama/llama-3.3-70b-instruct:free\n"
        "Model: [/dim]",
        end="",
    )
    new_model = input().strip()
    if new_model:
        save_setting("ARCDVN_MODEL", new_model)
        console.print(f"[green]✓ Model OpenRouter diubah ke: [bold]{new_model}[/bold][/green]\n")
        return new_model

    return current_model


def _show_git_diff():
    try:
        r = subprocess.run(
            ["git", "diff", "--stat"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=os.getcwd(),
        )
        diff_full = subprocess.run(
            ["git", "diff"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=os.getcwd(),
        )
        if not diff_full.stdout.strip():
            console.print("[dim]Tidak ada perubahan git yang belum di-commit.[/dim]\n")
            return

        if r.stdout.strip():
            console.print(
                Panel(
                    r.stdout.strip(),
                    title="[bold cyan]Git Diff Summary[/bold cyan]",
                    box=box.ROUNDED,
                    border_style="cyan",
                    padding=(0, 1),
                )
            )

        console.print(
            Panel(
                Syntax(diff_full.stdout[:20000], "diff", theme="monokai", line_numbers=False),
                title="[bold yellow]Git Diff[/bold yellow]",
                border_style="yellow",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )
    except Exception as e:
        console.print(f"[red]Gagal membaca git diff: {e}[/red]\n")


def _command_status(model, history):
    git_branch = _get_git_branch()
    table = Table(
        title="Status Sesi ARCDVN",
        title_style="bold cyan",
        border_style="cyan",
        box=box.ROUNDED,
        show_header=False,
        padding=(0, 2),
    )
    table.add_row("[dim]Project Directory[/dim]", os.getcwd())
    table.add_row("[dim]Git Branch[/dim]", f"[bold green]{git_branch}[/bold green]" if git_branch else "[dim]bukan git repo[/dim]")
    table.add_row("[dim]Model AI[/dim]", f"[bold cyan]{model}[/bold cyan]")
    table.add_row("[dim]Percakapan[/dim]", f"{len(history)} pesan di history")
    table.add_row("[dim]Detail Tool[/dim]", "[bold green]ON[/bold green]" if os.getenv("ARCDVNCLI_TOOL_DETAILS", "0") == "1" else "[dim]OFF[/dim]")
    table.add_row("[dim]Platform[/dim]", f"{sys.platform} • Python {sys.version.split()[0]}")
    console.print(table)


def _uninstall_self():
    console.print(
        Panel(
            "[bold red]⚠ Full Clean Uninstall ARCDVN Code[/bold red]\n\n"
            "Tindakan ini akan:\n"
            "• Menghapus seluruh file konfigurasi & API Key (~/.arcdvncli_env)\n"
            "• Menghapus seluruh riwayat percakapan (~/.arcdvncli_history)\n"
            "• Menghapus cache pembaruan (~/.arcdvncli_update_check)\n"
            "• Mencopot paket ARCDVN Code dari pipx",
            border_style="red",
            box=box.ROUNDED,
            padding=(0, 2),
        )
    )
    console.print("[dim]Apakah Anda yakin ingin menghapus bersih tanpa sisa? (y/N): [/dim]", end="")
    if input().strip().lower() != "y":
        console.print("[yellow]Pencopotan dibatalkan.[/yellow]\n")
        return False

    # 1. Hapus seluruh file konfigurasi & data di Home directory
    paths = [
        os.path.expanduser("~/.arcdvncli_env"),
        os.path.expanduser("~/.arcdvncli_history"),
        os.path.expanduser("~/.arcdvncli_update_check"),
    ]
    for p in paths:
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    console.print("[green]✓ Seluruh file konfigurasi, API Key, dan riwayat telah dibersihkan sampai bersih.[/green]")

    # 2. Hapus paket melalui pipx
    try:
        console.print("[dim]Mencopot paket aplikasi via pipx...[/dim]")
        subprocess.run(["pipx", "uninstall", "arcdvncli"], check=False)
        console.print("[bold green]✓ ARCDVN Code telah sukses dicopot sepenuhnya dari sistem.[/bold green]")
    except Exception:
        console.print("[dim]Ketik 'pipx uninstall arcdvncli' untuk menyelesaikan pencopotan paket.[/dim]")

    return True


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-V", "--version"):
        print(f"arcdvncli {__version__}")
        return

    if len(sys.argv) > 1 and sys.argv[1] in ("--uninstall", "uninstall"):
        _uninstall_self()
        return

    load_env()
    model = os.getenv("ARCDVN_MODEL", "google/gemini-2.5-flash")
    session = _prompt_session()

    banner(model)
    note = check_for_update()
    if note:
        console.print(f"[bold yellow]⬆ {escape(note)}[/bold yellow]\n")
    history = []

    def get_toolbar():
        git_branch = _get_git_branch()
        branch_str = f" • 🌿 {git_branch}" if git_branch else ""
        details = "ON" if os.getenv("ARCDVNCLI_TOOL_DETAILS", "0") == "1" else "OFF"
        short_model = model.split("/")[-1] if "/" in model else model
        return [
            ("class:bottom-toolbar.model", f" 🤖 {short_model} "),
            ("class:bottom-toolbar.dim", f"• 💬 {len(history)} msgs{branch_str} • Details: {details} "),
            ("class:bottom-toolbar.dim", "• [Tab] menu "),
        ]

    prompt_html = HTML("<b><ansicyan>ARCDVN</ansicyan></b> <ansigreen>❯</ansigreen> ")

    while True:
        try:
            _print_context_status(model, history)
            user = session.prompt(prompt_html, bottom_toolbar=get_toolbar).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Sampai jumpa![/dim]")
            break

        if not user:
            continue
        if user in {"/exit", "/quit"}:
            console.print("[dim]Sampai jumpa![/dim]")
            break

        if user == "/help":
            _command_help()
            continue

        if user == "/status":
            _command_status(model, history)
            continue

        if user == "/diff":
            _show_git_diff()
            continue

        if user == "/details":
            current = os.getenv("ARCDVNCLI_TOOL_DETAILS", "0") == "1"
            os.environ["ARCDVNCLI_TOOL_DETAILS"] = "0" if current else "1"
            status_text = "dinonaktifkan" if current else "diaktifkan"
            console.print(f"[cyan]Detail output tool {status_text}.[/cyan]\n")
            continue

        if user == "/clear":
            history.clear()
            banner(model)
            console.print("[green]Riwayat percakapan telah dibersihkan.[/green]\n")
            continue

        if user == "/settings":
            model = _settings()
            continue

        if user == "/uninstall":
            if _uninstall_self():
                break
            continue

        console.print(
            Panel(
                user,
                title="[bold cyan]👤 You[/bold cyan]",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

        try:
            answer = run_agent(user, history)

            short_model = model.split("/")[-1] if "/" in model else model
            console.print(
                Panel(
                    Markdown(answer),
                    title=f"[bold magenta]⚡ ARCDVN • {short_model}[/bold magenta]",
                    border_style="magenta",
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )
            console.print()

            if len(history) > 15:
                console.print(
                    Panel(
                        "[bold yellow]⚠ Konteks Percakapan Panjang[/bold yellow]\n"
                        f"[dim]Riwayat saat ini memuat {len(history)} pesan. "
                        "Kekuatan penalaran AI mungkin melemah. "
                        "Gunakan [/dim][cyan]/clear[/cyan][dim] untuk mereset memori.[/dim]",
                        border_style="yellow",
                        box=box.ROUNDED,
                        padding=(0, 1),
                    )
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
                    box=box.ROUNDED,
                    padding=(0, 1),
                )
            )


if __name__ == "__main__":
    main()
