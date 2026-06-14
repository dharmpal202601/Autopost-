"""
app/queue_manager.py
Persistent, SQLite-backed post queue with worker thread.

Lifecycle:
  - On start: re-loads pending/processing posts from DB (crash recovery)
  - Worker polls DB every 5 seconds for posts that are due
  - Processes one post at a time to respect API rate limits
  - On success: moves image to posted/, marks DB row as 'posted', adds DuplicateBlock
  - On failure: increments retry_count; after max_retries marks as 'failed'
  - Failed posts can be retried via retry_failed()

Thread-safety: Uses threading.Lock for publish operations; DB sessions are created
per-operation to avoid threading issues with SQLite.
"""

import os
import shutil
import threading
import time
import base64
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, List, Optional

from sqlalchemy import or_

from app.config import APP_CONFIG
from app.database import DuplicateBlock, Post, get_app_root, get_db
from app.logger import app_logger
from app.meta_publisher import meta_publisher
from app.scheduler import post_scheduler


def _get_posted_dir() -> Path:
    d = get_app_root() / "posted"
    d.mkdir(parents=True, exist_ok=True)
    return d


class QueueManager:
    """
    Manages the publication queue.
    Posts are persisted in SQLite so the queue survives restarts.
    """

    def __init__(self):
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._socketio = None

    # ─── Lifecycle ───────────────────────────────────────────────────────────

    def set_socketio(self, sio) -> None:
        """Inject Socket.IO for live queue updates to the dashboard."""
        self._socketio = sio

    def start(self) -> None:
        """Start the queue worker thread."""
        if self._running:
            return

        # Crash recovery: reset any 'processing' posts back to 'pending'
        self._reset_stuck_posts()

        self._running = True
        self._thread = threading.Thread(
            target=self._worker_loop,
            name="queue-worker",
            daemon=True,
        )
        self._thread.start()

        pending_count = self._count_pending()
        app_logger.info(
            f"Queue manager started. {pending_count} post(s) pending.",
            source="queue",
        )

    def stop(self) -> None:
        """Signal the worker to stop."""
        self._running = False
        app_logger.info("Queue manager stopped.", source="queue")

    @property
    def is_running(self) -> bool:
        return self._running and (self._thread is not None and self._thread.is_alive())

    # ─── Public API ──────────────────────────────────────────────────────────

    def add_post(
        self,
        wa_message_id: str,
        image_path: str,
        image_hash: str,
        caption: str,
        post_type: str = "live",
    ) -> None:
        """
        Called by WhatsAppMonitor when a new image post is detected.
        Creates a Post DB record and schedules it.
        """
        try:
            with get_db() as session:
                # Check if already queued
                existing = session.query(Post).filter_by(wa_message_id=wa_message_id).first()
                if existing:
                    return

                # Check duplicate by hash
                dup = session.query(DuplicateBlock).filter_by(image_hash=image_hash).first()
                if dup:
                    app_logger.warning(
                        f"Duplicate image (hash match) skipped: {image_hash[:16]}...",
                        source="queue",
                    )
                    return

                if post_type == "historical":
                    delay_until = post_scheduler.compute_historical_publish_time()
                elif post_type == "catchup":
                    # Missed posts (detected after restart): 2-minute gap between each
                    delay_until = post_scheduler.compute_catchup_publish_time()
                else:
                    delay_until = post_scheduler.compute_publish_time()
                    
                post = Post(
                    wa_message_id=wa_message_id,
                    image_path=image_path,
                    image_hash=image_hash,
                    caption=caption or "",
                    status="pending",
                    post_type=post_type,
                    retry_count=0,
                    delay_until=delay_until,
                )
                session.add(post)
                session.commit()

                if post_type == "catchup":
                    app_logger.info(
                        f"Catch-up post queued (missed while offline). "
                        f"Will publish at {delay_until.strftime('%H:%M:%S')} UTC.",
                        source="queue",
                    )
                elif post_type == "historical":
                    app_logger.info(
                        f"Historical post queued. "
                        f"Will publish at {delay_until.strftime('%H:%M:%S')} UTC.",
                        source="queue",
                    )
                else:
                    delay_secs = APP_CONFIG.get_publish_delay()
                    if delay_secs > 0:
                        app_logger.info(
                            f"Post queued with {delay_secs}s delay. "
                            f"Will publish at {delay_until.strftime('%H:%M:%S')} UTC.",
                            source="queue",
                        )
                    else:
                        app_logger.info("Post added to queue for immediate publishing.", source="queue")

            self._emit_queue_update()

        except Exception as e:
            app_logger.error(f"Failed to add post to queue: {e}", source="queue")

    def get_queue(self) -> List[dict]:
        """Return the current queue (pending + processing + recent failed) for the dashboard."""
        try:
            from app.database import SessionLocal
            session = SessionLocal()
            try:
                posts = (
                    session.query(Post)
                    .filter(Post.status.in_(["pending", "processing", "failed"]))
                    .order_by(Post.created_at.desc())
                    .limit(50)
                    .all()
                )
                result = []
                for p in posts:
                    d = p.to_dict()
                    # Attach thumbnail as base64 for dashboard display
                    if p.image_path and os.path.exists(p.image_path):
                        try:
                            with open(p.image_path, "rb") as f:
                                d["thumbnail_b64"] = base64.b64encode(f.read()).decode()
                        except Exception:
                            d["thumbnail_b64"] = None
                    else:
                        d["thumbnail_b64"] = None
                    result.append(d)
                return result
            finally:
                session.close()
        except Exception as e:
            app_logger.error(f"Error fetching queue: {e}", source="queue")
            return []

    def retry_failed(self) -> None:
        """Reset all 'failed' posts back to 'pending' for another attempt."""
        try:
            with get_db() as session:
                count = (
                    session.query(Post)
                    .filter_by(status="failed")
                    .update({
                        "status": "pending",
                        "retry_count": 0,
                        "delay_until": datetime.utcnow(),
                    })
                )
                session.commit()
                app_logger.info(f"Retrying {count} failed post(s).", source="queue")
            self._emit_queue_update()
        except Exception as e:
            app_logger.error(f"Error retrying failed posts: {e}", source="queue")

    # ─── Worker ──────────────────────────────────────────────────────────────

    def _worker_loop(self) -> None:
        """Main worker loop: checks for due posts every 5 seconds."""
        while self._running:
            try:
                with self._lock:
                    self._process_next()
            except Exception as e:
                app_logger.error(f"Queue worker error: {e}", source="queue")
            time.sleep(5)

    def _process_next(self) -> None:
        """Find the next due post and publish it."""
        from app.database import SessionLocal

        session = SessionLocal()
        try:
            post = (
                session.query(Post)
                .filter(
                    Post.status == "pending",
                    or_(
                        Post.delay_until.is_(None),
                        Post.delay_until <= datetime.utcnow(),
                    ),
                )
                .order_by(Post.created_at.asc())
                .first()
            )

            if not post:
                return

            # Mark as processing
            post.status = "processing"
            session.commit()

            post_id = post.id
            image_path = post.image_path
            caption = post.caption
            image_hash = post.image_hash
            wa_message_id = post.wa_message_id
        finally:
            session.close()

        self._emit_queue_update()
        app_logger.info(f"Processing post ID {post_id}...", source="queue")

        # Publish to configured platforms
        fb_result = {"success": False, "post_id": None, "error": "Not enabled"}
        ig_result = {"success": False, "post_id": None, "error": "Not enabled"}

        if APP_CONFIG.get("publishing.publish_to_facebook", True):
            if os.path.exists(image_path):
                fb_result = meta_publisher.publish_to_facebook(image_path, caption)
            else:
                fb_result = {"success": False, "post_id": None, "error": "Image file not found"}

        if APP_CONFIG.get("publishing.publish_to_instagram", True):
            if os.path.exists(image_path):
                ig_result = meta_publisher.publish_to_instagram(image_path, caption)
            else:
                ig_result = {"success": False, "post_id": None, "error": "Image file not found"}

        # Determine overall success
        fb_enabled = APP_CONFIG.get("publishing.publish_to_facebook", True)
        ig_enabled = APP_CONFIG.get("publishing.publish_to_instagram", True)

        any_success = (fb_enabled and fb_result["success"]) or (ig_enabled and ig_result["success"])
        all_failed = (
            (not fb_enabled or not fb_result["success"]) and
            (not ig_enabled or not ig_result["success"])
        )

        # Update DB record
        session = SessionLocal()
        try:
            post = session.get(Post, post_id)
            if post is None:
                return

            if any_success:
                post.fb_post_id = fb_result.get("post_id")
                post.ig_post_id = ig_result.get("post_id")
                post.posted_at = datetime.utcnow()
                post.status = "posted"

                # Add to DuplicateBlock
                existing_dup = session.query(DuplicateBlock).filter_by(image_hash=image_hash).first()
                if not existing_dup:
                    dup = DuplicateBlock(
                        image_hash=image_hash,
                        wa_message_id=wa_message_id,
                    )
                    session.add(dup)

                # Move image to posted/ folder
                new_path = self._move_to_posted(image_path)
                if new_path:
                    post.image_path = new_path

                app_logger.success(
                    f"Post {post_id} published successfully! "
                    f"FB: {fb_result.get('post_id')} | IG: {ig_result.get('post_id')}",
                    source="queue",
                )

            elif all_failed:
                post.retry_count += 1
                max_retries = APP_CONFIG.get_max_retries()
                if post.retry_count >= max_retries:
                    post.status = "failed"
                    app_logger.error(
                        f"Post {post_id} permanently failed after {post.retry_count} attempts.",
                        source="queue",
                    )
                else:
                    # Schedule retry with exponential back-off
                    retry_delay = 2 ** post.retry_count * 60   # 2min, 4min, 8min...
                    post.status = "pending"
                    post.delay_until = datetime.utcnow() + timedelta(seconds=retry_delay)
                    app_logger.warning(
                        f"Post {post_id} failed (attempt {post.retry_count}/{max_retries}). "
                        f"Retry in {retry_delay}s.",
                        source="queue",
                    )
            else:
                # Partial success (one platform succeeded, other failed)
                post.fb_post_id = fb_result.get("post_id")
                post.ig_post_id = ig_result.get("post_id")
                post.posted_at = datetime.utcnow()
                post.status = "posted"

                # Still block duplicate
                existing_dup = session.query(DuplicateBlock).filter_by(image_hash=image_hash).first()
                if not existing_dup:
                    session.add(DuplicateBlock(image_hash=image_hash, wa_message_id=wa_message_id))

                new_path = self._move_to_posted(image_path)
                if new_path:
                    post.image_path = new_path

                app_logger.warning(
                    f"Post {post_id} partially published. FB: {fb_result['success']}, IG: {ig_result['success']}",
                    source="queue",
                )

            session.commit()
        finally:
            session.close()

        self._emit_queue_update()

    # ─── Helpers ─────────────────────────────────────────────────────────────

    def _move_to_posted(self, image_path: str) -> Optional[str]:
        """Move an image file from downloads/ to posted/. Returns new path."""
        try:
            if not image_path or not os.path.exists(image_path):
                return None
            filename = os.path.basename(image_path)
            dest = str(_get_posted_dir() / filename)
            shutil.move(image_path, dest)
            return dest
        except Exception as e:
            app_logger.warning(f"Could not move image to posted/: {e}", source="queue")
            return None

    def _reset_stuck_posts(self) -> None:
        """On startup, reset any 'processing' posts to 'pending' (they were interrupted)."""
        try:
            with get_db() as session:
                count = (
                    session.query(Post)
                    .filter_by(status="processing")
                    .update({"status": "pending", "delay_until": datetime.utcnow()})
                )
                if count:
                    app_logger.info(
                        f"Reset {count} interrupted post(s) to pending.", source="queue"
                    )
        except Exception as e:
            app_logger.warning(f"Could not reset stuck posts: {e}", source="queue")

    def _count_pending(self) -> int:
        try:
            from app.database import SessionLocal
            session = SessionLocal()
            try:
                return session.query(Post).filter_by(status="pending").count()
            finally:
                session.close()
        except Exception:
            return 0

    def _emit_queue_update(self) -> None:
        """Push the current queue state to all connected dashboard clients."""
        if self._socketio is not None:
            try:
                self._socketio.emit("queue_update", self.get_queue(), namespace="/")
            except Exception:
                pass


# Module-level singleton
queue_manager = QueueManager()
