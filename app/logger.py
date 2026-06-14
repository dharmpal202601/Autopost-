"""
app/logger.py
Structured application logging with:
- Rotating file handler → logs/app.log
- Rich console output with colours
- SQLite storage for dashboard display
- Optional real-time Socket.IO emission to connected browsers
"""

import logging
import os
from contextlib import suppress
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional


def get_app_root() -> Path:
    return Path(__file__).resolve().parent.parent


class AppLogger:
    """
    Central logging hub.
    All components call app_logger.info() / .warning() / .error() / .success().
    Logs are written to file, console (via rich), DB, and optionally streamed to the
    browser dashboard via Socket.IO.
    """

    def __init__(self):
        self._socketio = None
        self._setup_file_logger()

    # ─── Setup ───────────────────────────────────────────────────────────────

    def _setup_file_logger(self) -> None:
        from app.database import get_data_dir
        root = get_data_dir()
        log_dir = root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "app.log"

        self._file_logger = logging.getLogger("wa_publisher")
        self._file_logger.setLevel(logging.DEBUG)

        if not self._file_logger.handlers:
            # Rotating file: 10 MB max, keep 5 backups
            fh = RotatingFileHandler(
                str(log_file),
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
            fmt = logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            fh.setFormatter(fmt)
            self._file_logger.addHandler(fh)

            # Console handler (basic, no Rich dependency for logging module)
            ch = logging.StreamHandler()
            ch.setLevel(logging.INFO)
            ch.setFormatter(fmt)
            self._file_logger.addHandler(ch)

    def set_socketio(self, sio) -> None:
        """Inject the Flask-SocketIO instance after the Flask app is created."""
        self._socketio = sio

    # ─── Public API ──────────────────────────────────────────────────────────

    def info(self, message: str, source: str = "system") -> None:
        self._log("INFO", message, source)

    def warning(self, message: str, source: str = "system") -> None:
        self._log("WARNING", message, source)

    def error(self, message: str, source: str = "system") -> None:
        self._log("ERROR", message, source)

    def success(self, message: str, source: str = "system") -> None:
        self._log("SUCCESS", message, source)

    # ─── Internal ────────────────────────────────────────────────────────────

    def _log(self, level: str, message: str, source: str) -> None:
        # 1. Write to file logger
        log_msg = f"[{source}] {message}"
        level_map = {
            "INFO": logging.INFO,
            "WARNING": logging.WARNING,
            "ERROR": logging.ERROR,
            "SUCCESS": logging.INFO,
        }
        self._file_logger.log(level_map.get(level, logging.INFO), log_msg)

        # 2. Persist to DB (suppress all errors so logging never crashes the app)
        entry = None
        with suppress(Exception):
            entry = self._save_to_db(level, message, source)

        # 3. Emit to browser via Socket.IO
        if self._socketio is not None:
            with suppress(Exception):
                payload = {
                    "level": level,
                    "message": message,
                    "source": source,
                    "time": datetime.utcnow().isoformat(),
                }
                # Use a background task so this is thread-safe
                self._socketio.emit("log", payload, namespace="/")

    def _save_to_db(self, level: str, message: str, source: str):
        """Persist log entry to SQLite. Returns the created entry or None."""
        from app.database import SessionLocal, LogEntry
        session = SessionLocal()
        try:
            entry = LogEntry(level=level, message=message, source=source)
            session.add(entry)
            session.commit()
            session.refresh(entry)
            return entry
        except Exception:
            session.rollback()
        finally:
            session.close()

    def get_recent(self, limit: int = 100) -> List[dict]:
        """Return the most recent log entries as dicts for the dashboard API."""
        with suppress(Exception):
            from app.database import SessionLocal, LogEntry
            session = SessionLocal()
            try:
                entries = (
                    session.query(LogEntry)
                    .order_by(LogEntry.timestamp.desc())
                    .limit(limit)
                    .all()
                )
                return [e.to_dict() for e in reversed(entries)]
            finally:
                session.close()
        return []


# Module-level singleton
app_logger = AppLogger()
