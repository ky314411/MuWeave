import json
import os
import tempfile
import threading
from pathlib import Path

import keyring
from cryptography.fernet import Fernet, InvalidToken
from keyring.errors import KeyringError


class SystemSecrets:
    def get(self, key: str) -> str | None:
        return keyring.get_password("MuWeave", key)

    def set(self, key: str, value: str):
        if value:
            keyring.set_password("MuWeave", key, value)
        elif self.get(key) is not None:
            keyring.delete_password("MuWeave", key)


class EncryptedFileSecrets:
    """Single-process container store; keep the master key on a separate volume."""

    def __init__(self, path: Path, key_path: Path):
        self.path, self.key_path = path, key_path
        self.lock = threading.RLock()

    def _cipher(self) -> Fernet:
        if not self.key_path.exists():
            if self.path.exists():
                raise KeyringError("Credential master key is missing; restore the key volume")
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.key_path.open("xb") as file:
                    os.chmod(self.key_path, 0o600)
                    file.write(Fernet.generate_key())
                    file.flush()
                    os.fsync(file.fileno())
            except FileExistsError:
                pass
        return Fernet(self.key_path.read_bytes())

    def _read(self, cipher: Fernet) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return json.loads(cipher.decrypt(self.path.read_bytes()))

    def get(self, key: str) -> str | None:
        with self.lock:
            try:
                return self._read(self._cipher()).get(key)
            except (OSError, ValueError, InvalidToken) as error:
                raise KeyringError(
                    "Cannot unlock container credentials; check the key and data volumes"
                ) from error

    def set(self, key: str, value: str):
        with self.lock:
            temporary = None
            try:
                cipher = self._cipher()
                records = self._read(cipher)
                if value:
                    records[key] = value
                else:
                    records.pop(key, None)
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as file:
                    temporary = Path(file.name)
                    os.chmod(temporary, 0o600)
                    file.write(cipher.encrypt(json.dumps(records).encode()))
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, self.path)
            except (OSError, ValueError, InvalidToken) as error:
                raise KeyringError(
                    "Cannot save container credentials; check the key and data volumes"
                ) from error
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
