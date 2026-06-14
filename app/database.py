"""
app/database.py
SQLAlchemy 2.x database models and session management for WA Channel Auto Publisher.
Uses SQLite with WAL mode for safe concurrent access from multiple threads.
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Generator

from sqlalchemy import (
    Column, DateTime, Integer, String, Text, event, create_engine, or_
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


# ─── Path helpers ────────────────────────────────────────────────────────────

def get_app_root() -> Path:
    """Return the project root directory (two levels above this file)."""
    return Path(__file__).resolve().parent.parent


def get_data_dir() -> Path:
    """Return the persistent data directory (defaults to app_root)."""
    data_dir_env = os.environ.get("DATA_DIR")
    if data_dir_env:
        return Path(data_dir_env).resolve()
    return get_app_root()


def _db_path() -> Path:
    data_dir = get_data_dir()
    db_dir = data_dir / "database"
    db_dir.mkdir(parents=True, exist_ok=True)
    return db_dir / "publisher.db"


DATABASE_URL = f"sqlite:///{_db_path()}"


# ─── SQLAlchemy setup ─────────────────────────────────────────────────────────

class Base(DeclarativeBase):
    pass


engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
)


# Enable WAL mode on every new connection for safe concurrent reads/writes
@event.listens_for(Engine, "connect")
def _set_wal_mode(dbapi_conn, connection_record):
    if isinstance(dbapi_conn, sqlite3.Connection):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


# ─── Models ───────────────────────────────────────────────────────────────────

class Post(Base):
    """Represents a WhatsApp channel post that has been detected and queued for publishing."""
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    wa_message_id = Column(String(200), unique=True, nullable=True, index=True)
    image_hash = Column(String(64), nullable=False, index=True)   # SHA-256 hex
    image_path = Column(String(500), nullable=True)               # Local filesystem path
    caption = Column(Text, nullable=True, default="")
    status = Column(String(20), nullable=False, default="pending", index=True)
    # status values: pending | processing | posted | failed | duplicate
    post_type = Column(String(20), nullable=False, default="live")
    # post_type values: live | historical
    retry_count = Column(Integer, nullable=False, default=0)
    fb_post_id = Column(String(200), nullable=True)
    ig_post_id = Column(String(200), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    posted_at = Column(DateTime, nullable=True)
    delay_until = Column(DateTime, nullable=True)   # Publish not before this time

    def to_dict(self) -> dict:
        filename = os.path.basename(self.image_path) if self.image_path else None
        return {
            "id": self.id,
            "wa_message_id": self.wa_message_id,
            "image_hash": self.image_hash,
            "image_path": self.image_path,
            "image_filename": filename,
            "image_url": f"/images/{filename}" if filename else None,
            "caption": self.caption,
            "status": self.status,
            "post_type": self.post_type,
            "retry_count": self.retry_count,
            "fb_post_id": self.fb_post_id,
            "ig_post_id": self.ig_post_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "delay_until": self.delay_until.isoformat() if self.delay_until else None,
        }


class LogEntry(Base):
    """Application log entry stored in DB for dashboard display."""
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    level = Column(String(10), nullable=False, default="INFO", index=True)
    # level values: INFO | WARNING | ERROR | SUCCESS
    message = Column(Text, nullable=False)
    source = Column(String(50), nullable=False, default="system", index=True)
    # source values: system | whatsapp | facebook | instagram | queue | config
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "level": self.level,
            "message": self.message,
            "source": self.source,
            "time": self.timestamp.isoformat() if self.timestamp else None,
        }


class Config(Base):
    """Key-value store for runtime configuration (non-secret settings)."""
    __tablename__ = "config"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)


class DuplicateBlock(Base):
    """Records of image hashes that have already been published — prevents re-posting."""
    __tablename__ = "duplicates"

    id = Column(Integer, primary_key=True, autoincrement=True)
    image_hash = Column(String(64), unique=True, nullable=False, index=True)
    wa_message_id = Column(String(200), nullable=True)
    blocked_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "image_hash": self.image_hash,
            "image_hash_short": self.image_hash[:16] + "...",
            "wa_message_id": self.wa_message_id,
            "blocked_at": self.blocked_at.isoformat() if self.blocked_at else None,
        }


# ─── DB Lifecycle ─────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create all tables if they don't exist. Safe to call multiple times."""
    # Ensure database directory exists
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_db() -> Generator[Session, None, None]:
    """Context manager for safe DB session usage with automatic commit/rollback."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
