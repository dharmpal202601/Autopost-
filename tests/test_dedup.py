import unittest
import os
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Import database models
from app.database import Base, Post, DuplicateBlock

# Setup in-memory test database
test_engine = create_engine("sqlite:///:memory:")
Base.metadata.create_all(bind=test_engine)
TestSessionLocal = sessionmaker(bind=test_engine)

# Custom context manager to replace get_db
from contextlib import contextmanager
@contextmanager
def mock_get_db():
    session = TestSessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

# Now we import the modules to test
with patch("app.database.SessionLocal", TestSessionLocal), \
     patch("app.database.get_db", mock_get_db):
    from app.queue_manager import QueueManager


class TestDedupAndQueue(unittest.TestCase):
    def setUp(self):
        # Clear database tables before each test
        self.session = TestSessionLocal()
        self.session.query(Post).delete()
        self.session.query(DuplicateBlock).delete()
        self.session.commit()

    def tearDown(self):
        self.session.close()

    @patch("app.database.SessionLocal", TestSessionLocal)
    @patch("app.database.get_db", mock_get_db)
    @patch("app.queue_manager.app_logger")
    @patch("app.queue_manager.post_scheduler")
    @patch("app.queue_manager.APP_CONFIG")
    def test_add_post_and_duplicate_prevention(self, mock_config, mock_scheduler, mock_logger):
        # Set up mocks
        mock_scheduler.compute_publish_time.return_value = datetime.utcnow()
        mock_config.get_publish_delay.return_value = 0
        
        qm = QueueManager()
        
        # 1. Add post successfully
        qm.add_post(
            wa_message_id="msg_001",
            image_path="downloads/image1.png",
            image_hash="hash_abc123",
            caption="Test Caption"
        )
        
        # Verify it exists in database
        posts = self.session.query(Post).all()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].wa_message_id, "msg_001")
        self.assertEqual(posts[0].image_hash, "hash_abc123")
        self.assertEqual(posts[0].status, "pending")
        
        # 2. Add duplicate message ID (should skip silently)
        qm.add_post(
            wa_message_id="msg_001",
            image_path="downloads/image1.png",
            image_hash="hash_abc123",
            caption="Test Caption"
        )
        
        posts = self.session.query(Post).all()
        self.assertEqual(len(posts), 1) # Still 1
        
        # 3. Add duplicate image hash with different message ID (should be blocked by DuplicateBlock table / checks)
        # First let's test block by inserting a DuplicateBlock
        dup_block = DuplicateBlock(image_hash="hash_abc123", wa_message_id="msg_001")
        self.session.add(dup_block)
        self.session.commit()
        
        qm.add_post(
            wa_message_id="msg_002",
            image_path="downloads/image2.png",
            image_hash="hash_abc123",
            caption="Another Caption"
        )
        
        posts = self.session.query(Post).all()
        self.assertEqual(len(posts), 1) # Still 1 post in DB, msg_002 is not added
        mock_logger.warning.assert_called_with(
            "Duplicate image (hash match) skipped: hash_abc123...",
            source="queue"
        )

    @patch("app.database.SessionLocal", TestSessionLocal)
    @patch("app.database.get_db", mock_get_db)
    @patch("app.queue_manager.app_logger")
    def test_reset_stuck_posts(self, mock_logger):
        # Insert a stuck (processing) post
        stuck_post = Post(
            wa_message_id="stuck_01",
            image_hash="hash_stuck",
            image_path="downloads/stuck.png",
            caption="Stuck",
            status="processing",
            created_at=datetime.utcnow() - timedelta(minutes=10)
        )
        self.session.add(stuck_post)
        self.session.commit()
        
        qm = QueueManager()
        qm._reset_stuck_posts()
        
        # Verify it was reset to pending
        post = self.session.query(Post).filter_by(wa_message_id="stuck_01").first()
        self.assertEqual(post.status, "pending")
        self.assertIsNotNone(post.delay_until)

    @patch("app.database.SessionLocal", TestSessionLocal)
    @patch("app.database.get_db", mock_get_db)
    @patch("app.queue_manager.os.path.exists")
    @patch("app.queue_manager.shutil.move")
    @patch("app.queue_manager._get_posted_dir")
    def test_move_to_posted(self, mock_get_posted_dir, mock_move, mock_exists):
        mock_exists.return_value = True
        from pathlib import Path
        mock_get_posted_dir.return_value = Path("/mock/posted")
        
        qm = QueueManager()
        new_path = qm._move_to_posted("/downloads/test.png")
        
        mock_move.assert_called_with("/downloads/test.png", str(Path("/mock/posted/test.png")))
        self.assertEqual(new_path, str(Path("/mock/posted/test.png")))
