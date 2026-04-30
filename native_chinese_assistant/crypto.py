"""At-rest encryption for sensitive project files (Cycle 13).

Encrypts/decrypts the quality store (which contains user-supplied original text +
LLM rewrites + ratings — sensitive). AES-GCM via the standard `cryptography` lib.

Format on disk:
    First 4 bytes: magic "NCG1"
    Next 12 bytes: nonce
    Rest: ciphertext + GCM tag
If the file does not start with "NCG1", we treat it as plaintext (legacy / never-encrypted)
and silently load it. On next save, it is written encrypted if a key is available.

Key resolution:
    1. `NCGA_DATA_KEY` env var (base64-encoded 32-byte key) — preferred for production
    2. `~/.local/share/ncga/data.key` — auto-generated on first run if no env var
    3. If neither available AND we can't write the keyfile, fall back to PLAINTEXT
       with a WARNING log (allows local dev to keep working without ceremony)
"""

from __future__ import annotations

import base64
import logging
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("ncga.crypto")

MAGIC = b"NCG1"
NONCE_SIZE = 12  # AES-GCM standard
KEY_SIZE = 32  # AES-256


def _user_key_path() -> Path:
    """XDG-compliant key file location."""
    base = os.environ.get("XDG_DATA_HOME", "").strip()
    if base:
        return Path(base) / "ncga" / "data.key"
    return Path.home() / ".local" / "share" / "ncga" / "data.key"


def resolve_key() -> bytes | None:
    """Returns 32 bytes or None (plaintext mode)."""
    env_key = os.environ.get("NCGA_DATA_KEY", "").strip()
    if env_key:
        try:
            key = base64.urlsafe_b64decode(env_key + "=" * (-len(env_key) % 4))
        except Exception:
            logger.warning("NCGA_DATA_KEY is not valid base64; ignoring")
            return None
        if len(key) != KEY_SIZE:
            logger.warning("NCGA_DATA_KEY must be 32 bytes (got %d); ignoring", len(key))
            return None
        return key

    keyfile = _user_key_path()
    if keyfile.is_file():
        try:
            raw = keyfile.read_bytes()
            if len(raw) == KEY_SIZE:
                return raw
        except OSError as exc:
            logger.warning("Could not read key file %s: %s", keyfile, exc)
    # Try to generate
    try:
        keyfile.parent.mkdir(parents=True, exist_ok=True)
        new_key = secrets.token_bytes(KEY_SIZE)
        # Write atomically with restrictive perms
        tmp = keyfile.with_suffix(".tmp")
        tmp.write_bytes(new_key)
        os.chmod(tmp, 0o600)
        tmp.replace(keyfile)
        logger.info("Generated new data key at %s (mode 600)", keyfile)
        return new_key
    except OSError as exc:
        logger.warning(
            "Could not auto-create key at %s (%s); quality store will be plaintext. "
            "Set NCGA_DATA_KEY=<32 random bytes base64> to enable encryption.",
            keyfile,
            exc,
        )
        return None


def encrypt(plaintext: bytes, key: bytes) -> bytes:
    """Returns MAGIC || nonce || ciphertext_with_tag."""
    nonce = secrets.token_bytes(NONCE_SIZE)
    ct = AESGCM(key).encrypt(nonce, plaintext, associated_data=MAGIC)
    return MAGIC + nonce + ct


def decrypt(blob: bytes, key: bytes) -> bytes:
    """Reverses encrypt(). Raises on tamper / wrong key / not encrypted."""
    if not blob.startswith(MAGIC):
        raise ValueError("not an NCG1-encrypted blob")
    if len(blob) < len(MAGIC) + NONCE_SIZE + 16:  # 16 = GCM tag
        raise ValueError("blob too short")
    nonce = blob[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
    ct = blob[len(MAGIC) + NONCE_SIZE :]
    return AESGCM(key).decrypt(nonce, ct, associated_data=MAGIC)


def is_encrypted(blob: bytes) -> bool:
    return blob.startswith(MAGIC)
