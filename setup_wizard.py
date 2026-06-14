#!/usr/bin/env python3
"""
setup_wizard.py
Interactive first-run setup wizard for WA Channel Auto Publisher.
Uses the `rich` library for a polished terminal UI.

Run this once after installation:
    python setup_wizard.py
"""

import getpass
import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Confirm, Prompt
    from rich.table import Table
    from rich import print as rprint
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.rule import Rule
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

console = Console() if RICH_AVAILABLE else None


def print_banner():
    if console:
        console.print(Panel.fit(
            "[bold green]WA Channel Auto Publisher[/bold green]\n"
            "[dim]v1.0.0 — First-Run Setup Wizard[/dim]",
            border_style="green",
            padding=(1, 4),
        ))
        console.print()
    else:
        print("=" * 50)
        print("  WA Channel Auto Publisher - Setup Wizard")
        print("=" * 50)
        print()


def step(num, title):
    if console:
        console.print(f"\n[bold cyan]Step {num}[/bold cyan] [white]{title}[/white]")
        console.print(Rule(style="dim"))
    else:
        print(f"\n--- Step {num}: {title} ---")


def ok(msg):
    if console:
        console.print(f"[green]OK:[/green] {msg}")
    else:
        print(f"[OK] {msg}")


def warn(msg):
    if console:
        console.print(f"[yellow]WARN:[/yellow]  {msg}")
    else:
        print(f"[WARN] {msg}")


def err(msg):
    if console:
        console.print(f"[red]ERROR:[/red] {msg}")
    else:
        print(f"[ERROR] {msg}")


def ask(prompt, default="", password=False):
    if password:
        val = getpass.getpass(f"  {prompt}: ")
        return val or default
    if console:
        return Prompt.ask(f"  [cyan]{prompt}[/cyan]", default=default) if default else \
               Prompt.ask(f"  [cyan]{prompt}[/cyan]")
    return input(f"  {prompt} [{default}]: ").strip() or default


def ask_confirm(prompt, default=True):
    if console:
        return Confirm.ask(f"  [cyan]{prompt}[/cyan]", default=default)
    ans = input(f"  {prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    if not ans:
        return default
    return ans.startswith("y")


def check_python_version():
    step(1, "Checking Python version")
    major, minor = sys.version_info[:2]
    if major < 3 or (major == 3 and minor < 10):
        err(f"Python {major}.{minor} detected. Python 3.10+ is required.")
        err("Download from: https://python.org/downloads/")
        sys.exit(1)
    ok(f"Python {major}.{minor} - compatible")


def install_requirements():
    step(2, "Installing Python dependencies")
    req_path = Path(__file__).parent / "requirements.txt"
    if not req_path.exists():
        warn("requirements.txt not found - skipping.")
        return

    if not ask_confirm("Install/update all required packages from requirements.txt?", default=True):
        warn("Skipping package installation.")
        return

    if console:
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as p:
            task = p.add_task("Installing packages...", total=None)
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", str(req_path), "--quiet"],
                capture_output=True,
                text=True,
            )
            p.update(task, completed=True)
    else:
        print("  Installing packages (this may take a minute)...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", str(req_path)],
        )

    if result.returncode == 0:
        ok("All packages installed successfully.")
    else:
        err("Some packages failed to install. Check the output above.")
        if hasattr(result, "stderr") and result.stderr:
            print(result.stderr[-500:])


def install_playwright():
    step(3, "Installing Playwright Chromium browser")
    if not ask_confirm("Install Playwright Chromium? (Required for WhatsApp monitoring)", default=True):
        warn("Skipping Playwright installation. Run: playwright install chromium")
        return
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=False,
    )
    if result.returncode == 0:
        ok("Playwright Chromium installed.")
    else:
        err("Playwright installation failed. Run: playwright install chromium")


def configure_whatsapp():
    step(4, "WhatsApp Channel Configuration")
    if console:
        console.print("  [dim]Example: https://whatsapp.com/channel/0029Va...[/dim]")
    url = ask("WhatsApp Channel URL")
    while url and not ("whatsapp.com/channel/" in url or "wa.me/channel/" in url):
        err("URL doesn't look like a WhatsApp Channel link.")
        if console:
            console.print("  [dim]It should contain 'whatsapp.com/channel/' or 'wa.me/channel/'[/dim]")
        url = ask("WhatsApp Channel URL")
    return url


def configure_meta():
    step(5, "Meta API Configuration")
    if console:
        console.print(Panel(
            "[bold]How to get your tokens:[/bold]\n\n"
            "1. Go to [link=https://developers.facebook.com]developers.facebook.com[/link]\n"
            "2. Create a new app → Business type\n"
            "3. Add [yellow]Facebook Login[/yellow] and [yellow]Instagram Graph API[/yellow] products\n"
            "4. Use [link=https://developers.facebook.com/tools/explorer]Graph API Explorer[/link] to get tokens\n"
            "5. Exchange for a long-lived Page token (see README for curl commands)\n\n"
            "[dim]All tokens are encrypted before storage.[/dim]",
            title="[cyan]Meta Setup Guide[/cyan]",
            border_style="dim",
        ))

    config = {}
    config["app_id"] = ask("Meta App ID (numeric)", default="")
    config["app_secret"] = ask("Meta App Secret", password=True)
    config["page_id"] = ask("Facebook Page ID (numeric)", default="")
    config["page_access_token"] = ask("Facebook Page Access Token (EAAxxxx...)", password=True)
    config["ig_user_id"] = ask("Instagram Business User ID (numeric)", default="")
    config["ig_access_token"] = ask("Instagram Access Token", password=True)
    config["imgbb_key"] = ask("imgbb API Key (optional — for IG image hosting fallback)", default="")
    return config


def configure_publishing():
    step(6, "Publishing Preferences")
    delay_choice = ask(
        "Publish delay? [0=immediate, 5=5min, 15=15min, or enter seconds]",
        default="0",
    )
    try:
        delay_secs = {
            "0": 0, "immediate": 0,
            "5": 300, "5min": 300,
            "15": 900, "15min": 900,
        }.get(delay_choice.lower(), int(delay_choice))
    except ValueError:
        delay_secs = 0

    pub_fb = ask_confirm("Publish to Facebook Page?", default=True)
    pub_ig = ask_confirm("Publish to Instagram Business?", default=True)
    return delay_secs, pub_fb, pub_ig


def save_config(wa_url, meta, delay_secs, pub_fb, pub_ig):
    step(7, "Encrypting and saving configuration")
    root = Path(__file__).parent

    # Ensure database dir exists for key storage
    (root / "database").mkdir(exist_ok=True)

    from app.config import AppConfig
    cfg = AppConfig()

    cfg.set("whatsapp.channel_url", wa_url)
    cfg.set("meta.app_id", meta["app_id"])
    cfg.set("meta.page_id", meta["page_id"])
    cfg.set("meta.ig_user_id", meta["ig_user_id"])
    cfg.set("publishing.delay_seconds", delay_secs)
    cfg.set("publishing.publish_to_facebook", pub_fb)
    cfg.set("publishing.publish_to_instagram", pub_ig)

    if meta["app_secret"]:
        cfg.set("meta.app_secret_encrypted", cfg.encrypt_token(meta["app_secret"]))
    if meta["page_access_token"]:
        cfg.set("meta.page_access_token_encrypted", cfg.encrypt_token(meta["page_access_token"]))
    if meta["ig_access_token"]:
        cfg.set("meta.ig_access_token_encrypted", cfg.encrypt_token(meta["ig_access_token"]))
    if meta["imgbb_key"]:
        cfg.set("image_host.imgbb_api_key_encrypted", cfg.encrypt_token(meta["imgbb_key"]))

    cfg.save()
    ok("Configuration encrypted and saved to config.json")


def verify_tokens():
    step(8, "Verifying API tokens")
    try:
        from app.meta_publisher import MetaPublisher
        publisher = MetaPublisher()
        result = publisher.verify_tokens()

        if result["facebook"]:
            ok("Facebook token: valid")
        else:
            warn("Facebook token: invalid or not configured")

        if result["instagram"]:
            ok("Instagram token: valid")
        else:
            warn("Instagram token: invalid or not configured")

        if result["errors"]:
            for e in result["errors"]:
                warn(f"  → {e}")
    except Exception as e:
        warn(f"Could not verify tokens: {e}")


def setup_whatsapp_login():
    step(9, "WhatsApp Web Login")
    if console:
        console.print(Panel(
            "The WhatsApp browser will open. [bold]Scan the QR code[/bold] with your phone:\n\n"
            "WhatsApp → [yellow]Linked Devices[/yellow] → [yellow]Link a Device[/yellow]\n\n"
            "[dim]Your session is saved locally — you won't need to scan again.[/dim]",
            title="[cyan]WhatsApp Login[/cyan]",
            border_style="dim",
        ))
    else:
        print("\n  Open the WhatsApp browser and scan the QR code with your phone.")
        print("  WhatsApp → Linked Devices → Link a Device")

    if ask_confirm("Open WhatsApp Web browser now for QR login?", default=True):
        try:
            from app.database import init_db
            init_db()
            from app.whatsapp_monitor import WhatsAppMonitor
            monitor = WhatsAppMonitor()
            ok("Starting browser... close it after scanning the QR code, then press Enter here.")
            import threading
            t = threading.Thread(target=monitor.start, daemon=True)
            t.start()
            input("\n  Press Enter after scanning the QR code and logging in...")
            monitor.stop()
            ok("WhatsApp session saved.")
        except ImportError:
            warn("Playwright not installed. Run: playwright install chromium")
        except Exception as e:
            warn(f"Could not start browser: {e}")
    else:
        warn("Skipped. You can scan the QR code when the app first starts.")


def install_startup_task():
    step(10, "Windows Startup Task")
    if ask_confirm("Register the app to start automatically at Windows login?", default=True):
        bat = Path(__file__).parent / "scripts" / "install_task.bat"
        if bat.exists():
            result = subprocess.run(str(bat), shell=True)
            if result.returncode == 0:
                ok("Startup task registered. App will start automatically on login.")
            else:
                warn("Failed to register task. Try running scripts/install_task.bat as Administrator.")
        else:
            warn("install_task.bat not found. Skipping.")
    else:
        warn("Skipped. You can run the app manually with: python main.py")


def print_summary(wa_url, meta):
    step(11, "Setup Complete!")
    if console:
        table = Table(show_header=True, header_style="bold cyan", border_style="dim")
        table.add_column("Setting", style="dim")
        table.add_column("Value")
        table.add_row("Channel URL", wa_url or "[red]Not set[/red]")
        table.add_row("Facebook Page ID", meta.get("page_id") or "[yellow]Not set[/yellow]")
        table.add_row("Instagram User ID", meta.get("ig_user_id") or "[yellow]Not set[/yellow]")
        table.add_row("Dashboard", "[cyan]http://localhost:5000[/cyan]")
        console.print(table)
        console.print()
        console.print(Panel(
            "[bold green]Setup complete![/bold green]\n\n"
            "Start the app:\n"
            "  [cyan]python main.py[/cyan]\n\n"
            "Open dashboard:\n"
            "  [cyan]http://localhost:5000[/cyan]\n\n"
            "[dim]Run setup_wizard.py again at any time to reconfigure.[/dim]",
            border_style="green",
        ))
    else:
        print("\nSetup complete!")
        print("  Start: python main.py")
        print("  Dashboard: http://localhost:5000")


def main():
    print_banner()

    try:
        check_python_version()
        install_requirements()
        install_playwright()

        wa_url = configure_whatsapp()
        meta = configure_meta()
        delay_secs, pub_fb, pub_ig = configure_publishing()

        save_config(wa_url, meta, delay_secs, pub_fb, pub_ig)
        verify_tokens()
        setup_whatsapp_login()
        install_startup_task()
        print_summary(wa_url, meta)

        if ask_confirm("\nStart the app now and open the dashboard?", default=True):
            webbrowser.open("http://localhost:5000")
            subprocess.Popen([sys.executable, "main.py"])

    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        sys.exit(0)
    except Exception as e:
        err(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
