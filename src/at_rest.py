"""Encryption at rest for private files and blobs (documents, project archives, mission evidence frames).

- AES-256-GCM (authenticated encryption, via the `cryptography` package). No homemade cipher.
- Envelope scheme: a master key (one per deployment, versioned by key id) derives a separate data key for each
  (user, workspace) pair with HKDF-SHA256. Data from one workspace cannot be opened with another workspace's key.
- Each ciphertext is bound to its record through associated data (the document/evidence/project id), so a blob
  copied to another record fails authentication.
- Blob format: MAGIC(5) | key_id(1) | nonce(12) | ciphertext+tag.

Master key sources, in order:
1. RESOLVE_MASTER_KEY (base64url, 32 bytes), the production path: set it from your secret manager.
2. A local development key file, created with mode 0600 outside Git. It is REFUSED in production mode
   (require_env=True), so production cannot silently run on a development key.
"""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

MAGIC = b"RSEV1"
CURRENT_KEY_ID = 1
_NONCE = 12


class SealError(Exception):
    """A blob could not be opened: wrong key, wrong scope, tampered data or unknown format."""


class SealedStore:
    def __init__(self, key_file: Path | str, require_env: bool = False, env: dict | None = None):
        env = os.environ if env is None else env
        self._keys: dict[int, bytes] = {}
        # Rotation: give the new master key a new version with RESOLVE_MASTER_KEY_ID and list earlier keys in
        # RESOLVE_MASTER_KEYS_OLD ("1:<key>,2:<key>"). New writes use the active version; older blobs stay readable.
        self.current_id = int(env.get("RESOLVE_MASTER_KEY_ID", str(CURRENT_KEY_ID)) or CURRENT_KEY_ID)
        if not 1 <= self.current_id <= 255:
            raise SealError("RESOLVE_MASTER_KEY_ID must be 1..255")
        raw = env.get("RESOLVE_MASTER_KEY", "").strip()
        if raw:
            self._keys[self.current_id] = self._decode(raw)
            self.source = "environment (RESOLVE_MASTER_KEY)"
        elif require_env:
            raise SealError("production requires RESOLVE_MASTER_KEY; refusing to start on a development key file")
        else:
            self._keys[self.current_id] = self._dev_key(Path(key_file))
            self.source = "development key file (local, mode 0600, not for production)"
        # previous key versions stay readable so rotation can re-seal data gradually
        for item in env.get("RESOLVE_MASTER_KEYS_OLD", "").split(","):
            item = item.strip()
            if ":" in item:
                kid, value = item.split(":", 1)
                if int(kid) == self.current_id:
                    raise SealError("an old key cannot reuse the active key version")
                self._keys[int(kid)] = self._decode(value)
        # Blind indexes (equality matching, e.g. obligation keys) must not change on rotation, or duplicates would no longer
        # be recognised. They stay on a pinned version (RESOLVE_BLIND_INDEX_KEY_ID, default: the oldest available key).
        self.blind_id = int(env.get("RESOLVE_BLIND_INDEX_KEY_ID", "0") or 0) or min(self._keys)
        if self.blind_id not in self._keys:
            raise SealError("the blind-index key version is not available")

    @staticmethod
    def _decode(value: str) -> bytes:
        try:
            key = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        except Exception as exc:
            raise SealError("master key is not valid base64") from exc
        if len(key) != 32:
            raise SealError("master key must be 32 bytes")
        return key

    @staticmethod
    def _dev_key(path: Path) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if os.name == "posix" and (path.stat().st_mode & 0o077):
                raise SealError(f"{path} must not be readable by other users (chmod 600)")
            return path.read_bytes()[:32]
        key = os.urandom(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(key)
        return key

    def _data_key(self, key_id: int, user_id: str, workspace_id: str) -> bytes:
        if not user_id or not workspace_id:
            raise SealError("user and workspace are required to derive a data key")
        info = b"resolve-data-key|" + user_id.encode() + b"|" + workspace_id.encode()
        return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(self._keys[key_id])

    def seal(self, plaintext: bytes, user_id: str, workspace_id: str, record_id: str) -> bytes:
        nonce = os.urandom(_NONCE)
        key = self._data_key(self.current_id, user_id, workspace_id)
        aad = b"|".join([record_id.encode(), user_id.encode(), workspace_id.encode()])
        body = AESGCM(key).encrypt(nonce, plaintext, aad)
        return MAGIC + bytes([self.current_id]) + nonce + body

    def key_version(self, blob: bytes) -> int:
        return blob[len(MAGIC)]

    def reseal(self, blob: bytes, user_id: str, workspace_id: str, record_id: str) -> bytes:
        """Re-encrypt a blob under the active key version (used by gradual rotation). Same scope and record binding."""
        if self.key_version(blob) == self.current_id:
            return blob
        return self.seal(self.open(blob, user_id, workspace_id, record_id), user_id, workspace_id, record_id)

    def open(self, blob: bytes, user_id: str, workspace_id: str, record_id: str) -> bytes:
        if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 1 + _NONCE + 16:
            raise SealError("not an encrypted RESOLVE blob")
        key_id = blob[len(MAGIC)]
        if key_id not in self._keys:
            raise SealError(f"key version {key_id} is not available")
        nonce = blob[len(MAGIC) + 1: len(MAGIC) + 1 + _NONCE]
        body = blob[len(MAGIC) + 1 + _NONCE:]
        key = self._data_key(key_id, user_id, workspace_id)
        aad = b"|".join([record_id.encode(), user_id.encode(), workspace_id.encode()])
        try:
            return AESGCM(key).decrypt(nonce, body, aad)
        except InvalidTag as exc:
            raise SealError("decryption failed: wrong key, wrong workspace, wrong record, or tampered data") from exc

    def blind_index(self, user_id: str, workspace_id: str, text: str) -> str:
        """Keyed, tenant-scoped index for matching without storing plaintext: HMAC-SHA256 under a derived blind-index key.
        Equal inputs in the same tenant give equal indexes; the database alone cannot be used to guess the input."""
        import hmac
        info = b"resolve-blind-index|" + user_id.encode() + b"|" + workspace_id.encode()
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(self._keys[self.blind_id])
        return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()

    @staticmethod
    def is_sealed(blob: bytes) -> bool:
        return blob.startswith(MAGIC)

    def fingerprint(self) -> str:
        """Non-secret identifier of the active key, safe to show in an export or the Privacy Center."""
        return hashlib.sha256(b"resolve-key-fingerprint|" + self._keys[self.current_id]).hexdigest()[:16]
