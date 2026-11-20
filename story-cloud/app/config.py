import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet


@dataclass
class Config:
    data_dir: Path
    admin_password: str
    encryption_secret: str
    cookie_secure: bool = True
    public_origin: str = ''
    telegram_api_id: int = 0
    telegram_api_hash: str = ''
    bridge_secret: str = ''
    snapshot_site_url: str = ''
    snapshot_site_token: str = ''
    free_mode: bool = False
    public_api_defaults: bool = True

    @classmethod
    def from_env(cls):
        cfg = cls(
            data_dir=Path(os.getenv('DATA_DIR', './data')),
            admin_password=os.getenv('ADMIN_PASSWORD', ''),
            encryption_secret=os.getenv('APP_ENCRYPTION_SECRET', ''),
            cookie_secure=os.getenv('COOKIE_SECURE', 'true').lower() == 'true',
            public_origin=os.getenv('PUBLIC_ORIGIN', '').rstrip('/'),
            telegram_api_id=int(os.getenv('TELEGRAM_API_ID', '0') or '0'),
            telegram_api_hash=os.getenv('TELEGRAM_API_HASH', ''),
            bridge_secret=os.getenv('STORY_BRIDGE_SECRET', ''),
            snapshot_site_url=os.getenv('SNAPSHOT_SITE_URL', '').rstrip('/'),
            snapshot_site_token=os.getenv('SNAPSHOT_SITE_TOKEN', ''),
            free_mode=os.getenv('FREE_MODE','false').lower()=='true',
            public_api_defaults=os.getenv('TELEGRAM_USE_PUBLIC_API','true').lower()=='true',
        )
        cfg.validate()
        return cfg

    def validate(self):
        if len(self.admin_password) < 12:
            raise RuntimeError('ADMIN_PASSWORD must have at least 12 characters')
        if len(self.encryption_secret) < 32:
            raise RuntimeError('APP_ENCRYPTION_SECRET must have at least 32 characters')
        if self.snapshot_site_url and (not self.snapshot_site_url.startswith('https://') or not self.bridge_secret or not self.snapshot_site_token):
            raise RuntimeError('Cloud snapshot storage requires HTTPS and service credentials')
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.data_dir.chmod(0o700)

    def cipher(self):
        key = base64.urlsafe_b64encode(hashlib.sha256(self.encryption_secret.encode()).digest())
        return Fernet(key)
