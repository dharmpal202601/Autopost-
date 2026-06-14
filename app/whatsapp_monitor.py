"""
app/whatsapp_monitor.py
Playwright-based WhatsApp Web channel monitor.

Architecture:
  - Launches a persistent Chromium context (headed, to avoid bot detection)
  - Session is saved to database/wa_session/ — user only needs to scan QR once
  - Polls the channel page every N seconds for new image posts
  - On detection: downloads image, computes hash, extracts caption, calls queue callback
  - Runs in a daemon thread; the main process manages lifecycle via start()/stop()

IMPORTANT: WhatsApp Web's DOM is updated frequently. Multiple selector fallbacks
are used throughout to be resilient to UI changes.
"""

import hashlib
import os
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Set

from app.config import APP_CONFIG
from app.database import DuplicateBlock, get_app_root, get_db
from app.logger import app_logger


def _get_downloads_dir() -> Path:
    from app.database import get_data_dir
    d = get_data_dir() / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_session_dir() -> Path:
    from app.database import get_data_dir
    d = get_data_dir() / "database" / "wa_session"
    d.mkdir(parents=True, exist_ok=True)
    return d


# WhatsApp Web DOM selectors (with fallbacks for resilience to UI changes)
_SELECTORS = {
    "qr_code": [
        "[data-testid='qrcode']",
        "canvas[aria-label*='QR']",
        "div[data-ref]",
    ],
    "logged_in": [
        "[data-testid='default-user']",
        "[data-testid='menu-bar-icon-side']",
        "#pane-side",
        "[data-testid='chatlist-header']",
    ],
    "messages_panel": [
        "#main",
        "[data-testid='conversation-panel-messages']",
        "[data-testid='msg-container']",
    ],
    "message_container": [
        "[data-testid='msg-container']",
        ".message-in",
        ".message-out",
        "div[data-id]",
    ],
    "image_in_message": [
        "img[src*='blob:']",
        "[data-testid='media-url-provider'] img",
        ".media-viewer-ghost-element img",
        "img.x3nfvp2",
    ],
    "caption_text": [
        "[data-testid='msg-container'] span.selectable-text",
        ".copyable-text span[dir='ltr']",
        "span[data-testid='msg-text'] span",
    ],
}

# Realistic Chrome user-agent
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class WhatsAppMonitor:
    """
    Monitors a WhatsApp channel for new image posts.
    Calls queue_callback(wa_message_id, image_path, image_hash, caption)
    for every new unique image discovered.
    """

    def __init__(self):
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._queue_callback: Optional[Callable] = None
        self._last_check_time: Optional[datetime] = None
        self._seen_ids: Set[str] = set()   # in-memory dedup for current session
        self._page = None
        self._is_first_scan: bool = True   # True until the first poll cycle completes
        self._qr_capture_running: bool = False

    # ─── Public API ──────────────────────────────────────────────────────────

    def set_queue_callback(self, fn: Callable) -> None:
        """Register the function to call when a new image post is detected."""
        self._queue_callback = fn

    def start(self) -> None:
        """Start the monitoring thread (non-blocking)."""
        if self._running:
            app_logger.warning("WhatsApp monitor already running.", source="whatsapp")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="wa-monitor",
            daemon=True,
        )
        self._thread.start()
        app_logger.info("WhatsApp monitor thread started.", source="whatsapp")

    def stop(self) -> None:
        """Signal the monitoring thread to stop."""
        self._running = False
        app_logger.info("WhatsApp monitor stopping...", source="whatsapp")

    @property
    def is_running(self) -> bool:
        return self._running and (self._thread is not None and self._thread.is_alive())

    @property
    def status(self) -> dict:
        return {
            "running": self.is_running,
            "waiting_for_qr": getattr(self, '_qr_capture_running', False),
            "channel_url": APP_CONFIG.get_wa_channel_url(),
            "last_check": self._last_check_time.isoformat() if self._last_check_time else None,
            "seen_count": len(self._seen_ids),
        }

    # ─── Monitor loop ────────────────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        """Main monitoring loop — runs in dedicated thread."""
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
        except ImportError:
            app_logger.error(
                "Playwright not installed. Run: pip install playwright && playwright install chromium",
                source="whatsapp",
            )
            self._running = False
            return

        try:
            with sync_playwright() as pw:
                app_logger.info("Launching WhatsApp Web browser...", source="whatsapp")

                # Clear stale lockfile from previous session (prevents profile lock errors)
                lockfile = _get_session_dir() / "lockfile"
                if lockfile.exists():
                    try:
                        lockfile.unlink()
                        app_logger.info("Cleared stale session lockfile.", source="whatsapp")
                    except Exception:
                        pass

                context = pw.chromium.launch_persistent_context(
                    user_data_dir=str(_get_session_dir()),
                    headless=False,               # Must be headed for WhatsApp
                    args=[
                        "--no-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-infobars",
                        "--start-minimized",
                        "--profile-directory=Default",
                    ],
                    user_agent=_USER_AGENT,
                    viewport={"width": 1280, "height": 800},
                )
                self._browser_context = context

                # Use first page or open a new one
                if context.pages:
                    page = context.pages[0]
                else:
                    page = context.new_page()
                self._page = page

                # Navigate to WhatsApp Web
                app_logger.info("Navigating to WhatsApp Web...", source="whatsapp")
                page.goto("https://web.whatsapp.com", timeout=60_000)

                # Handle QR code login if needed
                self._handle_login(page)

                if not self._running:
                    return

                # Navigate to the channel
                channel_url = APP_CONFIG.get_wa_channel_url()
                if channel_url and channel_url not in page.url:
                    app_logger.info(f"Navigating to channel: {channel_url}", source="whatsapp")
                    try:
                        page.goto(channel_url, timeout=30_000, wait_until="domcontentloaded")
                    except Exception as e:
                        app_logger.warning(f"Timeout navigating to channel, proceeding anyway: {e}", source="whatsapp")
                    
                    # Wait for the channel to load or 'View channel' button
                    time.sleep(5)
                    try:
                        # Sometimes there's a "View channel" prompt, try to click it
                        page.evaluate("""
                            () => {
                                let buttons = Array.from(document.querySelectorAll('div[role="button"]'));
                                let viewBtn = buttons.find(b => b.innerText && b.innerText.includes('View channel'));
                                if(viewBtn) viewBtn.click();
                            }
                        """)
                    except Exception:
                        pass
                    time.sleep(3)

                if not self._running:
                    return

                # Pre-load already-processed message IDs to avoid re-queueing on restart
                self._load_seen_ids()

                app_logger.success("WhatsApp monitor ready, polling for new posts...", source="whatsapp")

                # Poll loop
                while self._running:
                    try:
                        self._last_check_time = datetime.utcnow()
                        self._check_for_new_posts(page)
                    except Exception as e:
                        app_logger.warning(f"Error during post check: {e}", source="whatsapp")

                    # After first scan completes, mark as no longer first scan
                    if self._is_first_scan:
                        self._is_first_scan = False
                        app_logger.info("First scan complete. New images will now be treated as live.", source="whatsapp")

                    # Sleep with jitter to reduce bot detection risk
                    interval = APP_CONFIG.get_poll_interval()
                    jitter = random.randint(0, APP_CONFIG.get_poll_jitter())
                    sleep_time = interval + jitter
                    app_logger.info(f"Next check in {sleep_time}s...", source="whatsapp")

                    # Sleep in small increments so stop() can interrupt quickly
                    for _ in range(sleep_time):
                        if not self._running:
                            break
                        time.sleep(1)

        except Exception as e:
            app_logger.error(f"WhatsApp monitor crashed: {e}", source="whatsapp")
            self._running = False
        finally:
            self._browser_context = None
            self._page = None
            app_logger.info("WhatsApp monitor thread stopped.", source="whatsapp")

    def _handle_login(self, page) -> None:
        """Wait for QR scan if not already logged in."""
        from playwright.sync_api import TimeoutError as PlaywrightTimeout

        # Check if already logged in
        for sel in _SELECTORS["logged_in"]:
            if not self._running:
                return
            try:
                page.wait_for_selector(sel, timeout=3_000)
                app_logger.info("Already logged in to WhatsApp.", source="whatsapp")
                return
            except PlaywrightTimeout:
                continue

        # Not logged in — wait for QR
        for sel in _SELECTORS["qr_code"]:
            if not self._running:
                return
            try:
                page.wait_for_selector(sel, timeout=5_000)
                app_logger.warning(
                    "⚠ Please scan the QR code to log in.",
                    source="whatsapp",
                )
                
                self._qr_capture_running = True
                def capture_qr():
                    from app.database import get_data_dir
                    qr_path = get_data_dir() / "database" / "qr.png"
                    while self._qr_capture_running and self._running:
                        try:
                            qr_el = page.query_selector(sel)
                            if qr_el:
                                qr_el.screenshot(path=str(qr_path))
                        except Exception:
                            pass
                        time.sleep(2)
                    if qr_path.exists():
                        try:
                            qr_path.unlink()
                        except Exception:
                            pass
                            
                threading.Thread(target=capture_qr, daemon=True).start()
                break
            except PlaywrightTimeout:
                continue

        # Wait for login (up to 3 minutes) with periodic interrupt checks
        app_logger.info("Waiting for QR scan (up to 3 minutes)...", source="whatsapp")
        start_time = time.time()
        while self._running and (time.time() - start_time < 180):
            for sel in _SELECTORS["logged_in"]:
                if not self._running:
                    self._qr_capture_running = False
                    return
                try:
                    page.wait_for_selector(sel, timeout=2_000)
                    app_logger.success("WhatsApp login successful!", source="whatsapp")
                    self._qr_capture_running = False
                    return
                except PlaywrightTimeout:
                    continue
            time.sleep(1)

        self._qr_capture_running = False
        if not self._running:
            return

        app_logger.warning("Login not detected — proceeding anyway.", source="whatsapp")

    def _load_seen_ids(self) -> None:
        """Pre-populate _seen_ids from the database to avoid re-processing on restart."""
        try:
            from app.database import SessionLocal, Post
            session = SessionLocal()
            try:
                posts = session.query(Post.wa_message_id).filter(
                    Post.wa_message_id.isnot(None)
                ).all()
                self._seen_ids = {row[0] for row in posts if row[0]}
                app_logger.info(
                    f"Loaded {len(self._seen_ids)} known message IDs from DB.",
                    source="whatsapp",
                )
            finally:
                session.close()
        except Exception as e:
            app_logger.warning(f"Could not load seen IDs: {e}", source="whatsapp")

    # ─── Post detection ──────────────────────────────────────────────────────

    def _check_for_new_posts(self, page) -> None:
        """Scan the current page for new image messages and process each one."""
        try:
            # Scroll to bottom of the conversation panel to reveal latest messages
            page.evaluate("""
                () => {
                    let panel = document.querySelector('[data-testid="conversation-panel-messages"]');
                    if (panel) {
                        panel.scrollTop = panel.scrollHeight;
                        return;
                    }
                    let main = document.querySelector('#main');
                    if (main) {
                        let scrollers = Array.from(main.querySelectorAll('div')).filter(el => el.scrollHeight > el.clientHeight);
                        if (scrollers.length > 0) {
                            let scroller = scrollers[scrollers.length - 1];
                            scroller.scrollTop = scroller.scrollHeight;
                        }
                    }
                }
            """)
            time.sleep(1)
        except Exception:
            pass

        # Find all message elements
        message_elements = []
        for sel in _SELECTORS["message_container"]:
            try:
                elements = page.query_selector_all(sel)
                if elements:
                    message_elements = elements
                    break
            except Exception:
                continue
                
        if self._is_first_scan and message_elements:
            app_logger.info(f"First scan found {len(message_elements)} message elements on screen.", source="whatsapp")

        for element in message_elements:
            try:
                self._process_message_element(page, element)
            except Exception as e:
                app_logger.warning(f"Error processing message element: {e}", source="whatsapp")

    def _process_message_element(self, page, element) -> None:
        """Process a single message element — download and queue if it's a new image."""
        # Extract message ID
        msg_id = None
        for attr in ["data-id", "data-key", "id"]:
            try:
                msg_id = element.get_attribute(attr)
                if msg_id:
                    break
            except Exception:
                continue

        if not msg_id:
            # Generate a deterministic pseudo-ID from element content
            try:
                content = element.inner_text()[:50]
                msg_id = hashlib.md5(content.encode()).hexdigest()
            except Exception:
                return

        # Skip if already processed
        if msg_id in self._seen_ids:
            return
        self._seen_ids.add(msg_id)

        # Check for image in this message
        img_element = None
        for sel in _SELECTORS["image_in_message"]:
            try:
                img_element = element.query_selector(sel)
                if img_element:
                    break
            except Exception:
                continue

        if not img_element:
            # Text-only post — skip
            return

        # Get image source
        img_src = None
        for attr in ["src", "data-src"]:
            try:
                img_src = img_element.get_attribute(attr)
                if img_src:
                    break
            except Exception:
                continue

        if not img_src:
            app_logger.warning(f"Image element found but no src for msg {msg_id[:20]}", source="whatsapp")
            return

        # Download image
        try:
            image_bytes = self._download_image(page, img_src)
        except Exception as e:
            app_logger.warning(f"Could not download image for msg {msg_id}: {e}", source="whatsapp")
            return

        if not image_bytes or len(image_bytes) < 100:
            return

        # Compute hash
        image_hash = hashlib.sha256(image_bytes).hexdigest()

        # Duplicate check via DB
        try:
            with get_db() as session:
                dup = session.query(DuplicateBlock).filter_by(image_hash=image_hash).first()
                if dup:
                    app_logger.info(
                        f"Duplicate image detected (hash match), skipping msg {msg_id[:20]}",
                        source="whatsapp",
                    )
                    return
        except Exception:
            pass

        # Save image
        ext = ".jpg"
        filename = f"{image_hash[:16]}_{int(time.time())}{ext}"
        image_path = str(_get_downloads_dir() / filename)
        try:
            with open(image_path, "wb") as f:
                f.write(image_bytes)
        except Exception as e:
            app_logger.error(f"Could not save image: {e}", source="whatsapp")
            return

        # Extract caption
        caption = self._extract_caption(element)

        app_logger.success(
            f"New image post detected! msg_id={msg_id[:20]}, caption={repr(caption[:50])}",
            source="whatsapp",
        )

        # Determine post type:
        # - First scan after restart → 'catchup' (missed while offline, 2-min intervals)
        # - Subsequent scans → 'live' (real-time, publish immediately)
        post_type = "catchup" if self._is_first_scan else "live"
        if post_type == "catchup":
            app_logger.info(
                f"Catch-up image (missed while offline): msg_id={msg_id[:20]}",
                source="whatsapp",
            )

        # Fire the queue callback
        if self._queue_callback:
            try:
                self._queue_callback(msg_id, image_path, image_hash, caption, post_type)
            except Exception as e:
                app_logger.error(f"Queue callback error: {e}", source="whatsapp")

    def _download_image(self, page, img_src: str) -> bytes:
        """
        Download image bytes.
        For blob: URLs, uses Playwright's CDPSession to fetch the blob data.
        For regular URLs, uses requests with session cookies.
        """
        if img_src.startswith("blob:"):
            # Use CDP to fetch blob contents
            try:
                image_data = page.evaluate(
                    """
                    async (blobUrl) => {
                        const response = await fetch(blobUrl);
                        const buffer = await response.arrayBuffer();
                        const bytes = new Uint8Array(buffer);
                        let binary = '';
                        for (let i = 0; i < bytes.byteLength; i++) {
                            binary += String.fromCharCode(bytes[i]);
                        }
                        return btoa(binary);
                    }
                    """,
                    img_src,
                )
                import base64
                return base64.b64decode(image_data)
            except Exception as e:
                raise RuntimeError(f"Could not fetch blob URL: {e}")
        else:
            import requests
            # Get cookies from Playwright context
            cookies = self._browser_context.cookies() if self._browser_context else []
            cookie_dict = {c["name"]: c["value"] for c in cookies}
            headers = {
                "User-Agent": _USER_AGENT,
                "Referer": "https://web.whatsapp.com/",
            }
            response = requests.get(img_src, cookies=cookie_dict, headers=headers, timeout=30)
            response.raise_for_status()
            return response.content

    def _extract_caption(self, element) -> str:
        """Extract caption text from a message element."""
        for sel in _SELECTORS["caption_text"]:
            try:
                caption_el = element.query_selector(sel)
                if caption_el:
                    text = caption_el.inner_text().strip()
                    if text:
                        return text
            except Exception:
                continue

        # Fallback: get all text from element
        try:
            text = element.inner_text().strip()
            # Remove common WhatsApp UI text that isn't the caption
            lines = [l for l in text.split("\n") if l.strip() and len(l.strip()) > 1]
            return "\n".join(lines) if lines else ""
        except Exception:
            return ""


# Module-level singleton
wa_monitor = WhatsAppMonitor()
