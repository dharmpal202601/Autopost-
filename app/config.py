"""
app/config.py
Configuration management with Fernet AES-256 encryption.
Master key stored in Windows Credential Manager via keyring.
All sensitive tokens are stored encrypted in config.json.
"""

import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from cryptography.fernet import Fernet

# keyring is optional on some systems; fall back to file-based key storage
try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False

KEYRING_SERVICE = "WAAutoPublisher"
KEYRING_USERNAME = "master_key"
FALLBACK_KEY_FILE = ".master_key"   # inside database/ dir, restricted


def get_app_root() -> Path:
    return Path(__file__).resolve().parent.parent


class AppConfig:
    """
    Loads and manages config.json.
    Provides typed accessors for all configuration values.
    Handles Fernet encryption/decryption of sensitive tokens.
    """

    def __init__(self, config_path: Optional[str] = None):
        self._root = get_app_root()
        if config_path:
            self._config_path = Path(config_path)
        else:
            self._config_path = self._root / "config.json"

        self._data: dict = {}
        self._cipher: Optional[Fernet] = None

        self.load()
        self._init_cipher()

    # ─── Cipher ──────────────────────────────────────────────────────────────

    def _init_cipher(self) -> None:
        """Initialise Fernet cipher from stored master key, creating one if needed."""
        key = self._load_master_key()
        if not key:
            key = Fernet.generate_key().decode()
            self._store_master_key(key)
        self._cipher = Fernet(key.encode())

    def _load_master_key(self) -> Optional[str]:
        """Load master key from Windows Credential Manager or fallback file."""
        if KEYRING_AVAILABLE:
            try:
                key = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
                if key:
                    return key
            except Exception:
                pass

        # Fallback: read from file
        key_path = self._root / "database" / FALLBACK_KEY_FILE
        if key_path.exists():
            try:
                return key_path.read_text().strip()
            except Exception:
                pass
        return None

    def _store_master_key(self, key: str) -> None:
        """Persist master key to Windows Credential Manager or fallback file."""
        if KEYRING_AVAILABLE:
            try:
                keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, key)
                return
            except Exception:
                pass

        # Fallback: write to restricted file
        key_dir = self._root / "database"
        key_dir.mkdir(parents=True, exist_ok=True)
        key_path = key_dir / FALLBACK_KEY_FILE
        key_path.write_text(key)
        try:
            # Restrict permissions on Windows via icacls
            os.system(f'icacls "{key_path}" /inheritance:r /grant:r "%USERNAME%":F >nul 2>&1')
        except Exception:
            pass

    # ─── Load / Save ─────────────────────────────────────────────────────────

    def load(self) -> None:
        """Load config.json; copy from example if it doesn't exist."""
        if not self._config_path.exists():
            example = self._root / "config.json.example"
            if example.exists():
                shutil.copy(example, self._config_path)
            else:
                self._data = {}
                return
        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._data = {}

    def save(self) -> None:
        """Persist current config dict back to config.json."""
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2)

    # ─── Generic get / set ───────────────────────────────────────────────────

    def get(self, key: str, default: Any = None) -> Any:
        """
        Read a value using dot-notation key, e.g. 'meta.page_id'.
        Returns default if the key path doesn't exist.
        """
        parts = key.split(".")
        node = self._data
        for part in parts:
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, key: str, value: Any) -> None:
        """
        Write a value using dot-notation key.
        Creates intermediate dicts as needed.
        """
        parts = key.split(".")
        node = self._data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value
        self.save()

    # ─── Encryption helpers ──────────────────────────────────────────────────

    def encrypt_token(self, plaintext: str) -> str:
        """Encrypt a token string and return base64-encoded ciphertext."""
        if not plaintext:
            return ""
        if self._cipher is None:
            self._init_cipher()
        return self._cipher.encrypt(plaintext.encode()).decode()

    def decrypt_token(self, ciphertext: str) -> str:
        """Decrypt a previously encrypted token string."""
        if not ciphertext:
            return ""
        if self._cipher is None:
            self._init_cipher()
        try:
            return self._cipher.decrypt(ciphertext.encode()).decode()
        except Exception:
            return ""

    # ─── Typed accessors ─────────────────────────────────────────────────────

    def get_fb_token(self) -> str:
        encrypted = self.get("meta.page_access_token_encrypted", "")
        return self.decrypt_token(encrypted)

    def get_ig_token(self) -> str:
        encrypted = self.get("meta.ig_access_token_encrypted", "")
        return self.decrypt_token(encrypted)

    def get_app_secret(self) -> str:
        encrypted = self.get("meta.app_secret_encrypted", "")
        return self.decrypt_token(encrypted)

    def get_imgbb_key(self) -> str:
        encrypted = self.get("image_host.imgbb_api_key_encrypted", "")
        return self.decrypt_token(encrypted)

    def get_page_id(self) -> str:
        return self.get("meta.page_id", "")

    def get_ig_user_id(self) -> str:
        return self.get("meta.ig_user_id", "")

    def get_app_id(self) -> str:
        return self.get("meta.app_id", "")

    def get_wa_channel_url(self) -> str:
        return self.get("whatsapp.channel_url", "")

    def get_publish_delay(self) -> int:
        return int(self.get("publishing.delay_seconds", 0))

    def get_poll_interval(self) -> int:
        return int(self.get("whatsapp.poll_interval_seconds", 60))

    def get_poll_jitter(self) -> int:
        return int(self.get("whatsapp.poll_jitter_seconds", 30))

    def get_max_retries(self) -> int:
        return int(self.get("publishing.max_retries", 3))

    def get_dashboard_port(self) -> int:
        return int(self.get("dashboard.port", 5000))

    def get_dashboard_host(self) -> str:
        return self.get("dashboard.host", "127.0.0.1")

    def is_configured(self) -> bool:
        """Return True when all required non-placeholder fields are set."""
        required = [
            self.get_page_id(),
            self.get_ig_user_id(),
            self.get("meta.page_access_token_encrypted", ""),
            self.get("meta.ig_access_token_encrypted", ""),
            self.get_wa_channel_url(),
        ]
        defaults = [
            "YOUR_FACEBOOK_PAGE_ID",
            "YOUR_INSTAGRAM_USER_ID",
            "YOUR_META_APP_ID",
            "https://whatsapp.com/channel/YOUR_CHANNEL_ID",
        ]
        for val in required:
            if not val or val in defaults:
                return False
        return True


# Module-level singleton — imported by all other modules
APP_CONFIG = AppConfig()
