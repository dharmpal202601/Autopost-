import unittest
import tempfile
import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.config import AppConfig

class TestConfig(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for the config and database/master_key tests
        self.test_dir = tempfile.TemporaryDirectory()
        self.root_path = Path(self.test_dir.name)
        
        # We will write an example config file in the temp dir
        self.example_data = {
            "whatsapp": {
                "channel_url": "https://whatsapp.com/channel/YOUR_CHANNEL_ID",
                "poll_interval_seconds": 60,
                "poll_jitter_seconds": 30
            },
            "meta": {
                "app_id": "YOUR_META_APP_ID",
                "app_secret_encrypted": "",
                "page_id": "YOUR_FACEBOOK_PAGE_ID",
                "page_access_token_encrypted": "",
                "ig_user_id": "YOUR_INSTAGRAM_USER_ID",
                "ig_access_token_encrypted": ""
            },
            "publishing": {
                "delay_seconds": 0,
                "publish_to_facebook": True,
                "publish_to_instagram": True,
                "max_retries": 3
            },
            "dashboard": {
                "port": 5000,
                "host": "127.0.0.1"
            },
            "image_host": {
                "provider": "ngrok",
                "imgbb_api_key_encrypted": ""
            }
        }
        self.config_path = self.root_path / "config.json"
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.example_data, f, indent=2)

    def tearDown(self):
        self.test_dir.cleanup()

    @patch('app.config.get_app_root')
    @patch('app.config.KEYRING_AVAILABLE', False)
    def test_load_and_save(self, mock_get_root):
        mock_get_root.return_value = self.root_path
        # Instantiate AppConfig with temp config path
        config = AppConfig(config_path=str(self.config_path))
        
        # Test basic retrieval
        self.assertEqual(config.get("whatsapp.channel_url"), "https://whatsapp.com/channel/YOUR_CHANNEL_ID")
        self.assertEqual(config.get_page_id(), "YOUR_FACEBOOK_PAGE_ID")
        self.assertEqual(config.get_publish_delay(), 0)
        
        # Test set and save
        config.set("whatsapp.poll_interval_seconds", 120)
        self.assertEqual(config.get_poll_interval(), 120)
        
        # Verify file on disk updated
        with open(self.config_path, "r", encoding="utf-8") as f:
            saved_data = json.load(f)
        self.assertEqual(saved_data["whatsapp"]["poll_interval_seconds"], 120)

    @patch('app.config.get_app_root')
    @patch('app.config.KEYRING_AVAILABLE', False)
    def test_encryption(self, mock_get_root):
        mock_get_root.return_value = self.root_path
        # Create database dir inside temp dir to hold fallback key
        os.makedirs(self.root_path / "database", exist_ok=True)
        
        config = AppConfig(config_path=str(self.config_path))
        token = "super-secret-token"
        
        encrypted = config.encrypt_token(token)
        self.assertNotEqual(encrypted, token)
        self.assertTrue(len(encrypted) > 0)
        
        decrypted = config.decrypt_token(encrypted)
        self.assertEqual(decrypted, token)
        
        # Test empty token
        self.assertEqual(config.encrypt_token(""), "")
        self.assertEqual(config.decrypt_token(""), "")

    @patch('app.config.get_app_root')
    @patch('app.config.KEYRING_AVAILABLE', False)
    def test_is_configured(self, mock_get_root):
        mock_get_root.return_value = self.root_path
        os.makedirs(self.root_path / "database", exist_ok=True)
        
        config = AppConfig(config_path=str(self.config_path))
        
        # Initially not configured because tokens are empty/placeholders
        self.assertFalse(config.is_configured())
        
        # Set dummy encrypted tokens and details
        config.set("meta.page_access_token_encrypted", config.encrypt_token("fb_token"))
        config.set("meta.ig_access_token_encrypted", config.encrypt_token("ig_token"))
        config.set("whatsapp.channel_url", "https://whatsapp.com/channel/real_channel")
        config.set("meta.page_id", "real_page_id")
        config.set("meta.ig_user_id", "real_ig_user_id")
        
        self.assertTrue(config.is_configured())
