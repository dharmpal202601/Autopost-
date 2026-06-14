"""
app/meta_publisher.py
Meta Graph API v25.0 publisher for Facebook Pages and Instagram Business accounts.

Facebook:  POST /{page_id}/photos  (direct multipart file upload — no public URL needed)
Instagram: 2-step container method — image must be at a publicly accessible URL.
           The image_host module handles the URL provisioning.

Features:
  - Exponential backoff retries (3 attempts)
  - HTTP 429 rate-limit detection and back-off
  - IG quota monitoring
  - Token verification helpers
"""

import time
import os
from typing import Optional

import requests

from app.config import APP_CONFIG
from app.image_host import image_host, ImageHostError
from app.logger import app_logger

GRAPH_URL = "https://graph.facebook.com/v25.0"
RETRY_DELAYS = [5, 30, 120]   # seconds between retry attempts


class MetaPublisher:
    """Handles all Meta Graph API publishing operations."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    # ─── Facebook ────────────────────────────────────────────────────────────

    def publish_to_facebook(self, image_path: str, caption: str) -> dict:
        """
        Upload image directly to Facebook Page as a photo post.
        Uses multipart/form-data — no public URL required.

        Returns: {"success": bool, "post_id": str|None, "error": str|None}
        """
        page_id = APP_CONFIG.get_page_id()
        token = APP_CONFIG.get_fb_token()

        if not page_id or not token:
            msg = "Facebook Page ID or access token not configured."
            app_logger.error(msg, source="facebook")
            return {"success": False, "post_id": None, "error": msg}

        url = f"{GRAPH_URL}/{page_id}/photos"
        last_error = "Unknown network or API error"

        for attempt, delay in enumerate(RETRY_DELAYS + [None], start=1):
            try:
                with open(image_path, "rb") as f:
                    filename = os.path.basename(image_path)
                    resp = self._session.post(
                        url,
                        files={"source": (filename, f, "image/jpeg")},
                        data={"caption": caption, "access_token": token},
                        timeout=60,
                    )

                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", 3600))
                    app_logger.warning(
                        f"Facebook rate limit hit. Waiting {retry_after}s...",
                        source="facebook",
                    )
                    time.sleep(min(retry_after, 3600))
                    continue

                resp.raise_for_status()
                data = resp.json()

                if "id" in data:
                    post_id = data["id"]
                    app_logger.success(
                        f"Published to Facebook! Post ID: {post_id}", source="facebook"
                    )
                    return {"success": True, "post_id": post_id, "error": None}
                else:
                    error_msg = data.get("error", {}).get("message", "Unknown error")
                    app_logger.warning(
                        f"Facebook publish attempt {attempt} failed: {error_msg}",
                        source="facebook",
                    )

            except requests.exceptions.RequestException as e:
                detailed_error = str(e)
                if hasattr(e, 'response') and e.response is not None:
                    try:
                        fb_err = e.response.json().get("error", {})
                        detailed_error = fb_err.get("message", e.response.text)
                    except Exception:
                        pass
                
                last_error = detailed_error
                app_logger.warning(
                    f"Facebook network error (attempt {attempt}): {detailed_error}", source="facebook"
                )

            if delay is not None:
                app_logger.info(f"Retrying Facebook in {delay}s...", source="facebook")
                time.sleep(delay)
            else:
                break

        error_msg = f"All Facebook publish attempts failed. Last error: {last_error}"
        app_logger.error(error_msg, source="facebook")
        return {"success": False, "post_id": None, "error": error_msg}

    # ─── Instagram ───────────────────────────────────────────────────────────

    def publish_to_instagram(self, image_path: str, caption: str) -> dict:
        """
        Publish image to Instagram Business via 2-step container method.
        Requires a publicly accessible image URL (handled by image_host).

        Returns: {"success": bool, "post_id": str|None, "error": str|None}
        """
        ig_user_id = APP_CONFIG.get_ig_user_id()
        token = APP_CONFIG.get_ig_token()

        if not ig_user_id or not token:
            msg = "Instagram User ID or access token not configured."
            app_logger.error(msg, source="instagram")
            return {"success": False, "post_id": None, "error": msg}

        # Get public URL for the image
        public_url = None
        try:
            public_url = image_host.get_public_url(image_path)
        except ImageHostError as e:
            error_msg = str(e)
            app_logger.error(f"Cannot host image for Instagram: {error_msg}", source="instagram")
            return {"success": False, "post_id": None, "error": error_msg}

        for attempt, delay in enumerate(RETRY_DELAYS + [None], start=1):
            try:
                # Step 1: Create media container
                container_id = self._create_ig_container(ig_user_id, token, public_url, caption)
                if not container_id:
                    raise RuntimeError("Failed to create Instagram media container.")

                # Step 1b: Poll container status (async processing)
                ready = self._poll_ig_container(container_id, token)
                if not ready:
                    raise RuntimeError("Instagram container processing timed out or errored.")

                # Step 2: Publish container
                post_id = self._publish_ig_container(ig_user_id, token, container_id)
                if not post_id:
                    raise RuntimeError("Failed to publish Instagram container.")

                app_logger.success(
                    f"Published to Instagram! Post ID: {post_id}", source="instagram"
                )
                if public_url:
                    image_host.cleanup(public_url)
                return {"success": True, "post_id": post_id, "error": None}

            except requests.exceptions.HTTPError as e:
                if e.response is not None and e.response.status_code == 429:
                    retry_after = int(e.response.headers.get("Retry-After", 3600))
                    app_logger.warning(
                        f"Instagram rate limit hit. Waiting {retry_after}s...",
                        source="instagram",
                    )
                    time.sleep(min(retry_after, 3600))
                    continue
                app_logger.warning(
                    f"Instagram HTTP error (attempt {attempt}): {e}", source="instagram"
                )
            except Exception as e:
                app_logger.warning(
                    f"Instagram publish attempt {attempt} failed: {e}", source="instagram"
                )

            if delay is not None:
                app_logger.info(f"Retrying Instagram in {delay}s...", source="instagram")
                time.sleep(delay)
            else:
                break

        if public_url:
            image_host.cleanup(public_url)
        error_msg = "All Instagram publish attempts failed."
        app_logger.error(error_msg, source="instagram")
        return {"success": False, "post_id": None, "error": error_msg}

    def _create_ig_container(
        self, ig_user_id: str, token: str, image_url: str, caption: str
    ) -> Optional[str]:
        """Step 1 of IG publishing: create a media container. Returns container ID."""
        resp = self._session.post(
            f"{GRAPH_URL}/{ig_user_id}/media",
            data={
                "image_url": image_url,
                "caption": caption,
                "access_token": token,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        container_id = data.get("id")
        if container_id:
            app_logger.info(
                f"IG container created: {container_id}", source="instagram"
            )
        return container_id

    def _poll_ig_container(self, container_id: str, token: str, max_polls: int = 12) -> bool:
        """Poll container status until FINISHED or ERROR. Returns True if ready."""
        for i in range(max_polls):
            try:
                resp = self._session.get(
                    f"{GRAPH_URL}/{container_id}",
                    params={"fields": "status_code,status", "access_token": token},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                status_code = data.get("status_code", "")

                if status_code == "FINISHED":
                    return True
                if status_code in ("ERROR", "EXPIRED"):
                    app_logger.error(
                        f"IG container {container_id} status: {status_code} — {data.get('status', '')}",
                        source="instagram",
                    )
                    return False

                app_logger.info(
                    f"IG container status: {status_code} (poll {i+1}/{max_polls})",
                    source="instagram",
                )
                time.sleep(10)
            except Exception as e:
                app_logger.warning(f"Error polling IG container: {e}", source="instagram")
                time.sleep(10)

        app_logger.warning("IG container polling timed out.", source="instagram")
        return False

    def _publish_ig_container(
        self, ig_user_id: str, token: str, container_id: str
    ) -> Optional[str]:
        """Step 2 of IG publishing: publish the container. Returns post ID."""
        resp = self._session.post(
            f"{GRAPH_URL}/{ig_user_id}/media_publish",
            data={"creation_id": container_id, "access_token": token},
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("id")

    # ─── Quota ───────────────────────────────────────────────────────────────

    def check_ig_quota(self) -> dict:
        """
        Query the Instagram content publishing limit.
        Returns: {"used": int, "limit": int, "remaining": int}
        """
        ig_user_id = APP_CONFIG.get_ig_user_id()
        token = APP_CONFIG.get_ig_token()
        default = {"used": 0, "limit": 25, "remaining": 25}

        if not ig_user_id or not token:
            return default

        try:
            resp = self._session.get(
                f"{GRAPH_URL}/{ig_user_id}/content_publishing_limit",
                params={"fields": "config,quota_usage", "access_token": token},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [{}])[0]
            config = data.get("config", {})
            quota_usage = data.get("quota_usage", 0)
            limit = config.get("quota_total", 25)
            return {
                "used": quota_usage,
                "limit": limit,
                "remaining": max(0, limit - quota_usage),
            }
        except Exception as e:
            app_logger.warning(f"Could not fetch IG quota: {e}", source="instagram")
            return default

    # ─── Token verification ──────────────────────────────────────────────────

    def verify_tokens(self) -> dict:
        """
        Test both tokens with lightweight API calls.
        Returns: {"facebook": bool, "instagram": bool, "errors": list[str]}
        """
        errors = []
        fb_ok = False
        ig_ok = False

        # Facebook
        fb_token = APP_CONFIG.get_fb_token()
        if fb_token:
            try:
                resp = self._session.get(
                    f"{GRAPH_URL}/me",
                    params={"access_token": fb_token, "fields": "id,name"},
                    timeout=10,
                )
                if resp.status_code == 200 and "id" in resp.json():
                    fb_ok = True
                else:
                    err = resp.json().get("error", {}).get("message", "Unknown FB error")
                    errors.append(f"Facebook: {err}")
            except Exception as e:
                errors.append(f"Facebook: {e}")
        else:
            errors.append("Facebook: no token configured")

        # Instagram
        ig_token = APP_CONFIG.get_ig_token()
        ig_user_id = APP_CONFIG.get_ig_user_id()
        if ig_token and ig_user_id:
            try:
                resp = self._session.get(
                    f"{GRAPH_URL}/{ig_user_id}",
                    params={"fields": "id,name,username", "access_token": ig_token},
                    timeout=10,
                )
                if resp.status_code == 200 and "id" in resp.json():
                    ig_ok = True
                else:
                    err = resp.json().get("error", {}).get("message", "Unknown IG error")
                    errors.append(f"Instagram: {err}")
            except Exception as e:
                errors.append(f"Instagram: {e}")
        else:
            errors.append("Instagram: user ID or token not configured")

        return {"facebook": fb_ok, "instagram": ig_ok, "errors": errors}


# Module-level singleton
meta_publisher = MetaPublisher()
