"""
app/image_host.py
Provides a publicly-accessible HTTPS URL for local image files.
Required because Instagram's Content Publishing API only accepts URLs, not file uploads.

Strategy (in order of preference):
  1. pyngrok – exposes the local Flask static server via an ngrok tunnel (free, no account needed)
  2. imgbb  – uploads image to imgbb.com and returns the URL (requires free API key)

The Flask dashboard already exposes  GET /images/<filename>  which serves files
from the downloads/ and posted/ directories. ngrok tunnels to that same server.
"""

import os
import time
from pathlib import Path
from typing import Optional

from app.config import APP_CONFIG
from app.logger import app_logger

# Optional pyngrok import
try:
    from pyngrok import ngrok, conf as ngrok_conf
    NGROK_AVAILABLE = True
except ImportError:
    NGROK_AVAILABLE = False

try:
    import requests as _requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False


class ImageHostError(Exception):
    """Raised when no image hosting method succeeds."""


class ImageHostManager:
    """
    Manages temporary public hosting of local image files.
    The ngrok tunnel, once established, is reused for the entire session.
    """

    def __init__(self):
        self._ngrok_tunnel = None          # pyngrok tunnel object
        self._ngrok_public_url: str = ""   # e.g. https://abc123.ngrok-free.app
        self._flask_port: int = 5000       # matches dashboard Flask port
        self._imgbb_urls: list[str] = []   # track uploaded imgbb URLs for cleanup

    def set_flask_port(self, port: int) -> None:
        """Call this after Flask app is created to set the correct port."""
        self._flask_port = port

    # ─── Public API ──────────────────────────────────────────────────────────

    def get_public_url(self, local_path: str) -> str:
        """
        Return a publicly accessible HTTPS URL for the given local image file.
        Tries ngrok first, then imgbb.
        Raises ImageHostError if both fail.
        """
        filename = os.path.basename(local_path)

        # 1. Try ngrok
        if NGROK_AVAILABLE:
            try:
                public_base = self._ensure_ngrok_tunnel()
                url = f"{public_base}/images/{filename}"
                app_logger.info(f"Image hosted via ngrok: {url}", source="system")
                return url
            except Exception as e:
                app_logger.warning(f"ngrok failed ({e}), trying imgbb...", source="system")

        # 2. Try imgbb
        imgbb_key = APP_CONFIG.get_imgbb_key()
        if imgbb_key and REQUESTS_AVAILABLE:
            try:
                url = self._upload_to_imgbb(local_path, imgbb_key)
                app_logger.info(f"Image hosted via imgbb: {url}", source="system")
                return url
            except Exception as e:
                app_logger.error(f"imgbb upload failed: {e}", source="system")

        raise ImageHostError(
            "Could not host image publicly. "
            "Install pyngrok (pip install pyngrok) or set an imgbb API key in Settings."
        )

    def cleanup(self, url: str) -> None:
        """Clean up an imgbb URL after publishing (ngrok URLs don't need cleanup)."""
        if "ibb.co" in url or "imgbb.com" in url:
            # imgbb doesn't offer a free deletion API; just remove from tracking list
            if url in self._imgbb_urls:
                self._imgbb_urls.remove(url)

    def stop(self) -> None:
        """Kill the ngrok tunnel gracefully on shutdown."""
        if NGROK_AVAILABLE and self._ngrok_tunnel:
            try:
                ngrok.disconnect(self._ngrok_tunnel.public_url)
                app_logger.info("ngrok tunnel closed.", source="system")
            except Exception:
                pass
            finally:
                self._ngrok_tunnel = None
                self._ngrok_public_url = ""

    # ─── Internal ────────────────────────────────────────────────────────────

    def _ensure_ngrok_tunnel(self) -> str:
        """Start ngrok tunnel if not already running; return the public base URL."""
        if self._ngrok_public_url:
            return self._ngrok_public_url

        app_logger.info("Starting ngrok tunnel...", source="system")
        tunnel = ngrok.connect(self._flask_port, "http")
        self._ngrok_tunnel = tunnel
        self._ngrok_public_url = tunnel.public_url.replace("http://", "https://")
        app_logger.success(f"ngrok tunnel ready: {self._ngrok_public_url}", source="system")
        return self._ngrok_public_url

    def _upload_to_imgbb(self, local_path: str, api_key: str) -> str:
        """Upload image to imgbb and return the direct display URL."""
        import base64
        import requests

        with open(local_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        response = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": api_key, "image": image_data},
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()

        if not data.get("success"):
            raise ImageHostError(f"imgbb upload failed: {data}")

        url = data["data"]["url"]
        self._imgbb_urls.append(url)
        return url


# Module-level singleton
image_host = ImageHostManager()
