from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path

from cryptography.fernet import Fernet

from .config import AppConfig


KEYCHAIN_SERVICE = "video-refiner-webapp"


class SecretStore:
    def __init__(self, config: AppConfig):
        self.secure_dir = config.app_home / "secure"
        self.secure_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.secure_dir, 0o700)
        self.fernet_key_file = self.secure_dir / "fallback-fernet.key"
        self.encrypted_file = self.secure_dir / "api-keys.json.enc"

    def set_api_key(self, profile_id: str, api_key: str) -> str:
        if not api_key:
            return "unchanged"
        if self._set_keychain(profile_id, api_key):
            return "macos_keychain"
        data = self._read_encrypted()
        data[profile_id] = api_key
        self._write_encrypted(data)
        return "encrypted_file"

    def get_api_key(self, profile_id: str) -> str | None:
        key = self._get_keychain(profile_id)
        if key:
            return key
        return self._read_encrypted().get(profile_id)

    def delete_api_key(self, profile_id: str) -> None:
        if platform.system() == "Darwin":
            subprocess.run(
                ["security", "delete-generic-password", "-s", KEYCHAIN_SERVICE, "-a", profile_id],
                capture_output=True,
                text=True,
            )
        data = self._read_encrypted()
        if profile_id in data:
            del data[profile_id]
            self._write_encrypted(data)

    def _set_keychain(self, profile_id: str, api_key: str) -> bool:
        if platform.system() != "Darwin":
            return False
        result = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                profile_id,
                "-w",
                api_key,
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _get_keychain(self, profile_id: str) -> str | None:
        if platform.system() != "Darwin":
            return None
        result = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE, "-a", profile_id],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    def _fernet(self) -> Fernet:
        if not self.fernet_key_file.exists():
            self.fernet_key_file.write_bytes(Fernet.generate_key())
            os.chmod(self.fernet_key_file, 0o600)
        return Fernet(self.fernet_key_file.read_bytes())

    def _read_encrypted(self) -> dict[str, str]:
        if not self.encrypted_file.exists():
            return {}
        try:
            raw = self._fernet().decrypt(self.encrypted_file.read_bytes()).decode("utf-8")
            return json.loads(raw)
        except Exception:
            return {}

    def _write_encrypted(self, data: dict[str, str]) -> None:
        token = self._fernet().encrypt(json.dumps(data, ensure_ascii=False).encode("utf-8"))
        self.encrypted_file.write_bytes(token)
        os.chmod(self.encrypted_file, 0o600)

