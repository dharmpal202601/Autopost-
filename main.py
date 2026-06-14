#!/usr/bin/env python3
"""
main.py
WA Channel Auto Publisher — Application entry point.

Starts three concurrent components:
  1. WhatsApp Web monitor (Playwright, browser thread)
  2. Queue worker (publishes to Meta APIs)
  3. Flask + Socket.IO dashboard (localhost:5000)

Usage:
  python main.py                  # normal start
  python main.py --no-browser     # dashboard only (no WhatsApp monitoring)
  python main.py --install-browser # install Playwright Chromium then exit
  python main.py --port 8080      # use a different dashboard port
"""

import argparse
import signal
import sys
import threading
import webbrowser
from pathlib import Path

# Ensure project root is on sys.path regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parent))


def create_directories() -> None:
    """Create the required folder structure if it doesn't already exist."""
    root = Path(__file__).resolve().parent
    for folder in ["downloads", "posted", "logs", "database", "database/wa_session"]:
        (root / folder).mkdir(parents=True, exist_ok=True)


def install_browser() -> None:
    """Install Playwright Chromium (used by the installer's post-install step)."""
    import subprocess
    print("Installing Playwright Chromium browser...")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
    )
    if result.returncode == 0:
        print("[OK] Playwright Chromium installed successfully.")
    else:
        print("[ERROR] Playwright installation failed. Please run manually:")
        print("   playwright install chromium")
    sys.exit(result.returncode)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="WA Channel Auto Publisher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Start dashboard only — skip WhatsApp browser automation",
    )
    parser.add_argument(
        "--install-browser",
        action="store_true",
        help="Install Playwright Chromium and exit (used by installer)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: from config or 5000)",
    )
    args = parser.parse_args()

    if args.install_browser:
        install_browser()

    print("=" * 55)
    print("  WA Channel Auto Publisher v1.0.0")
    print("=" * 55)

    # ── Setup ──────────────────────────────────────────────────────────────
    create_directories()

    from app.database import init_db
    init_db()
    print("[OK] Database initialised")

    from app.config import APP_CONFIG
    from app.logger import app_logger

    # ── Instantiate components ─────────────────────────────────────────────
    from app.image_host import image_host
    from app.meta_publisher import MetaPublisher
    from app.queue_manager import QueueManager
    from app.whatsapp_monitor import WhatsAppMonitor

    wa_monitor = WhatsAppMonitor()
    publisher = MetaPublisher()
    queue_mgr = QueueManager()

    # Wire: WhatsApp monitor → queue manager
    wa_monitor.set_queue_callback(queue_mgr.add_post)

    # ── Create Flask app ───────────────────────────────────────────────────
    from dashboard.server import create_app, socketio

    flask_app = create_app(
        wa_monitor=wa_monitor,
        queue_mgr=queue_mgr,
        publisher=publisher,
    )

    # Tell image_host what port Flask is on (for ngrok tunnel)
    import os
    env_port = os.environ.get("PORT")
    port = args.port or (int(env_port) if env_port else None) or APP_CONFIG.get_dashboard_port()
    image_host.set_flask_port(port)

    # ── Graceful shutdown ──────────────────────────────────────────────────
    _stop_event = threading.Event()

    def shutdown(signum=None, frame=None) -> None:
        if _stop_event.is_set():
            return  # already shutting down
        _stop_event.set()
        app_logger.info("Shutdown signal received. Stopping...", source="system")
        wa_monitor.stop()
        queue_mgr.stop()
        image_host.stop()
        print("\nWA Auto Publisher stopped.")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # ── Start queue worker ─────────────────────────────────────────────────
    queue_mgr.start()
    app_logger.info("Queue manager started.", source="system")
    print("[OK] Queue manager started")

    # ── Start WhatsApp monitor ─────────────────────────────────────────────
    if not args.no_browser:
        channel_url = APP_CONFIG.get_wa_channel_url()
        if channel_url and "YOUR_CHANNEL_ID" not in channel_url:
            wa_monitor.start()
            app_logger.info("WhatsApp monitor starting...", source="whatsapp")
            print("[OK] WhatsApp monitor starting (check browser window for QR if needed)")
        else:
            app_logger.warning(
                "WhatsApp channel URL not configured. "
                "Open the dashboard Settings tab to configure it.",
                source="system",
            )
            print("[WARN] WhatsApp channel URL not set - configure it in the dashboard")
    else:
        print("[WARN] WhatsApp monitor skipped (--no-browser flag)")

    # ── Start dashboard ────────────────────────────────────────────────────
    host = "0.0.0.0" if env_port else APP_CONFIG.get_dashboard_host()
    dashboard_url = f"http://{host}:{port}"

    app_logger.success(f"Dashboard running at {dashboard_url}", source="system")

    print()
    print(f"Dashboard: {dashboard_url}")
    print("   Press Ctrl+C to stop.\n")

    # Auto-open dashboard in browser after short delay (Flask needs time to start)
    def _open_browser():
        import time
        time.sleep(2)
        try:
            webbrowser.open(dashboard_url)
            app_logger.info(f"Dashboard opened in browser: {dashboard_url}", source="system")
        except Exception:
            pass

    if not env_port:
        threading.Thread(target=_open_browser, daemon=True, name="browser-opener").start()

    # Block on Flask-SocketIO
    try:
        socketio.run(
            flask_app,
            host=host,
            port=port,
            debug=False,
            use_reloader=False,
            log_output=False,
            allow_unsafe_werkzeug=True,
        )
    except KeyboardInterrupt:
        shutdown()


if __name__ == "__main__":
    main()
