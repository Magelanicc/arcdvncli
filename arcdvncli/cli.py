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
from .llm import (
    load_env,
    save_setting,
    get_provider_config,
    get_active_provider,
    test_connection,
    PROVIDERS,
)
from .updater import check_for_update

console = Console()

COMMAND_MAP = {
    "/help": "Tampilkan panduan dan daftar perintah",
    "/settings": "Atur Provider AI (Gemini / OpenRouter), API Key, dan Model",
    "/status": "Lihat status proyek, git, context window, provider, dan model",
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

    cfg = get_provider_config()
    model_name = model or cfg["model"]
    prov_name = cfg["name"]
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
        f"[dim]Provider:[/dim] [bold yellow]{prov_name}[/bold yellow]  •  "
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
        "[cyan]/settings[/cyan] [dim]atur provider & model[/dim] • "
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
    cfg = get_provider_config()
    details = os.getenv("ARCDVNCLI_TOOL_DETAILS", "0") == "1"
    git_branch = _get_git_branch()
    branch_str = f" [dim]│ Branch[/dim] [green]{git_branch}[/green]" if git_branch else ""
    short_model = model.split("/")[-1] if "/" in model else model
    console.print(
        f"[dim]Provider[/dim] [yellow]{cfg['name']}[/yellow]  "
        f"[dim]│ Model[/dim] [cyan]{short_model}[/cyan]  "
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
    while True:
        cfg = get_provider_config()
        current_provider = cfg["id"]
        current_key = cfg["key"]
        current_model = cfg["model"]
        current_base = cfg["base_url"]

        masked_key = (
            f"{current_key[:6]}...{current_key[-4:]}"
            if len(current_key) > 10
            else ("[green]Tersimpan[/green]" if current_key else "[red](belum disetel)[/red]")
        )

        google_key = os.getenv("ARCDVN_GOOGLE_API_KEY") or (os.getenv("ARCDVN_API_KEY") if current_provider == "google" else "")
        openrouter_key = os.getenv("ARCDVN_OPENROUTER_API_KEY") or (os.getenv("ARCDVN_API_KEY") if current_provider == "openrouter" else "")

        google_status = "[green]✓ Siap[/green]" if google_key else "[yellow]Belum ada key[/yellow]"
        openrouter_status = "[green]✓ Siap[/green]" if openrouter_key else "[yellow]Belum ada key[/yellow]"

        active_badge_google = " [bold cyan](AKTIF)[/bold cyan]" if current_provider == "google" else ""
        active_badge_or = " [bold cyan](AKTIF)[/bold cyan]" if current_provider == "openrouter" else ""

        console.print(
            Panel(
                f"[dim]Provider Aktif :[/dim]  [bold yellow]{cfg['name']}[/bold yellow]\n"
                f"[dim]Model Aktif    :[/dim]  [bold cyan]{current_model}[/bold cyan]\n"
                f"[dim]Endpoint       :[/dim]  [dim]{current_base}[/dim]\n"
                f"[dim]API Key Status :[/dim]  {masked_key}\n\n"
                f"[dim]Daftar Provider Tersimpan:[/dim]\n"
                f"  • Google Gemini : {google_status}{active_badge_google}\n"
                f"  • OpenRouter    : {openrouter_status}{active_badge_or}",
                title="[bold cyan]Pengaturan Provider & Model AI[/bold cyan]",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(0, 2),
            )
        )

        console.print(
            "[bold cyan]Pilihan Menu Pengaturan:[/bold cyan]\n"
            "  [bold yellow]1[/bold yellow] - Ganti Provider (Google Gemini / OpenRouter)\n"
            "  [bold yellow]2[/bold yellow] - Atur API Key untuk provider aktif\n"
            "  [bold yellow]3[/bold yellow] - Ganti Model untuk provider aktif\n"
            "  [bold yellow]4[/bold yellow] - Tes Koneksi API (Read-only ping)\n"
            "  [bold yellow]5[/bold yellow] - Atur Endpoint / Base URL Kustom\n"
            "  [bold yellow]0[/bold yellow] - Selesai & Kembali ke Chat\n"
        )

        console.print("[dim]Pilih nomor (0-5): [/dim]", end="")
        try:
            choice = input().strip()
        except (KeyboardInterrupt, EOFError):
            break

        if choice in ("", "0", "exit", "kembali"):
            break

        elif choice == "1":
            console.print("\n[bold cyan]Pilih Provider:[/bold cyan]")
            console.print("  [yellow]1[/yellow] - Google Gemini (Default: gemini-3.8-flash)")
            console.print("  [yellow]2[/yellow] - OpenRouter (Default: google/gemini-2.5-flash)")
            console.print("[dim]Pilih (1 atau 2): [/dim]", end="")
            try:
                p_choice = input().strip()
            except (KeyboardInterrupt, EOFError):
                continue

            if p_choice == "1":
                save_setting("ARCDVN_PROVIDER", "google")
                # If model was openrouter-style, reset to gemini default
                if "/" in current_model or not current_model:
                    save_setting("ARCDVN_MODEL", PROVIDERS["google"]["default_model"])
                console.print("[green]✓ Provider aktif beralih ke: Google Gemini[/green]\n")
            elif p_choice == "2":
                save_setting("ARCDVN_PROVIDER", "openrouter")
                if "/" not in current_model:
                    save_setting("ARCDVN_MODEL", PROVIDERS["openrouter"]["default_model"])
                console.print("[green]✓ Provider aktif beralih ke: OpenRouter[/green]\n")
            else:
                console.print("[yellow]Pilihan dibatalkan.[/yellow]\n")

        elif choice == "2":
            target_env = cfg["env_key"] or "ARCDVN_API_KEY"
            console.print(f"\n[dim]Masukkan API Key untuk [bold]{cfg['name']}[/bold] ({target_env}): [/dim]", end="")
            try:
                new_key = getpass.getpass("").strip()
            except Exception:
                new_key = input().strip()

            if new_key:
                save_setting(target_env, new_key)
                save_setting("ARCDVN_API_KEY", new_key)
                console.print(f"[green]✓ API Key untuk {cfg['name']} berhasil disimpan.[/green]\n")
            else:
                console.print("[yellow]Tidak ada perubahan API Key.[/yellow]\n")

        elif choice == "3":
            hint = PROVIDERS.get(current_provider, {}).get("models_hint", "")
            console.print(f"\n[dim]Model saat ini: [bold cyan]{current_model}[/bold cyan][/dim]")
            if hint:
                console.print(f"[dim]Rekomendasi model {cfg['name']}: {hint}[/dim]")
            console.print("[dim]Masukkan Model ID baru (kosongkan untuk batal): [/dim]", end="")
            try:
                new_model = input().strip()
            except (KeyboardInterrupt, EOFError):
                continue

            if new_model:
                save_setting("ARCDVN_MODEL", new_model)
                console.print(f"[green]✓ Model diubah ke: [bold]{new_model}[/bold][/green]\n")
            else:
                console.print("[yellow]Model tidak diubah.[/yellow]\n")

        elif choice == "4":
            console.print(f"\n[dim]Menguji koneksi ke {cfg['name']} ({cfg['model']})...[/dim]")
            success, msg = test_connection()
            if success:
                console.print(
                    Panel(
                        f"[bold green]✓ {msg}[/bold green]",
                        border_style="green",
                        box=box.ROUNDED,
                        padding=(0, 2),
                    )
                )
            else:
                console.print(
                    Panel(
                        f"[bold red]✗ Gagal:[/bold red] {msg}\n\n"
                        "[dim]Periksa kembali API Key, kuota akun, atau koneksi internet Anda.[/dim]",
                        border_style="red",
                        box=box.ROUNDED,
                        padding=(0, 2),
                    )
                )
            console.print()

        elif choice == "5":
            console.print(f"\n[dim]Endpoint default: {cfg['default_base_url']}[/dim]")
            console.print(f"[dim]Endpoint saat ini: {cfg['base_url']}[/dim]")
            console.print("[dim]Masukkan URL baru (atau ketik 'default' untuk mereset): [/dim]", end="")
            try:
                new_url = input().strip()
            except (KeyboardInterrupt, EOFError):
                continue

            if new_url.lower() == "default":
                save_setting("ARCDVN_BASE_URL", "")
                console.print(f"[green]✓ Endpoint di-reset ke default: {cfg['default_base_url']}[/green]\n")
            elif new_url:
                save_setting("ARCDVN_BASE_URL", new_url.rstrip("/"))
                console.print(f"[green]✓ Endpoint diubah ke: {new_url}[/green]\n")

    updated_cfg = get_provider_config()
    return updated_cfg["model"]


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
    cfg = get_provider_config()
    git_branch = _get_git_branch()
    has_key = bool(cfg["key"])
    key_str = "[bold green]Tersimpan[/bold green]" if has_key else "[bold red]Belum disetel[/bold red]"

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
    table.add_row("[dim]Provider AI[/dim]", f"[bold yellow]{cfg['name']}[/bold yellow]")
    table.add_row("[dim]Model AI[/dim]", f"[bold cyan]{model}[/bold cyan]")
    table.add_row("[dim]Endpoint API[/dim]", cfg["base_url"])
    table.add_row("[dim]Status API Key[/dim]", key_str)
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
    cfg = get_provider_config()
    model = cfg["model"]
    session = _prompt_session()

    banner(model)
    note = check_for_update()
    if note:
        console.print(f"[bold yellow]⬆ {escape(note)}[/bold yellow]\n")
    history = []

    def get_toolbar():
        c = get_provider_config()
        git_branch = _get_git_branch()
        branch_str = f" • 🌿 {git_branch}" if git_branch else ""
        details = "ON" if os.getenv("ARCDVNCLI_TOOL_DETAILS", "0") == "1" else "OFF"
        short_model = model.split("/")[-1] if "/" in model else model
        return [
            ("class:bottom-toolbar.model", f" 🤖 {c['name']}: {short_model} "),
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
            banner(model)
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

            c = get_provider_config()
            short_model = model.split("/")[-1] if "/" in model else model
            console.print(
                Panel(
                    Markdown(answer),
                    title=f"[bold magenta]⚡ ARCDVN • {c['name']} • {short_model}[/bold magenta]",
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
