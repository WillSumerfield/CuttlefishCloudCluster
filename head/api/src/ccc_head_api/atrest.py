"""Encryption at rest for the one secret head.db holds in recoverable form: a User's W&B key.

Everything else in head.db is one-way (argon2id passwords, sha256 session tokens) — nothing else ever
needs to come back. A W&B key is a third-party credential the Head must hand to `wandb` verbatim inside
every Spawn, so it can't be hashed. AES-256-GCM, with a 32-byte keyfile beside the database.

**Be clear about what this buys.** The Head decrypts unattended, so the keyfile sits next to head.db and
anything that can read one can read the other. This protects a head.db that leaks *on its own* — a
copied database, a backup (`cuttle backup` deliberately excludes the keyfile), a pulled disk — not a
compromised Head.

Failure degrades to "no key", never to an error: a restored backup (keyfile gone by design) must not 500
the Account page or block submission over it. An unreadable key reads as unset — the User re-pastes it.
"""

import logging
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("ccc.api")

_KEY_BYTES = 32   # AES-256
_NONCE_BYTES = 12  # GCM's native nonce width; the blob is nonce || ciphertext+tag

# Said once per process, not once per read: a Head whose keyfile is missing has every stored key
# unreadable, and one warning per Spawn submission would drown the log in a restated fact.
_warned = False


def _warn_once(msg: str) -> None:
    global _warned
    if not _warned:
        log.warning("%s — stored W&B keys read as unset until their owners re-paste them", msg)
        _warned = True


def key_path() -> Path:
    """Where the at-rest key lives: beside head.db, or wherever CCC_HEAD_SECRET_KEY points.

    `db` is imported here rather than at module scope because db imports *this* module for its
    wandb_key helpers. Reading db.DB_PATH each call (not at import) also means a test that redirects
    the database redirects the keyfile with it.
    """
    from . import db
    override = os.environ.get("CCC_HEAD_SECRET_KEY")
    return Path(override) if override else db.DB_PATH.parent / "secret.key"


def _load_key() -> bytes | None:
    """The at-rest key, or None if there is no keyfile. Never creates one — only `encrypt` may."""
    path = key_path()
    return path.read_bytes() if path.exists() else None


def _ensure_key() -> bytes:
    """The at-rest key, minting one on first use. 0600 *before* the bytes land in it, so the key is
    never briefly world-readable."""
    key = _load_key()
    if key is not None:
        return key
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Two requests both found no keyfile and both tried to mint one. O_EXCL is what makes that
        # safe: the loser takes the winner's key rather than overwriting it, which would strand every
        # blob written in between.
        return path.read_bytes()
    key = os.urandom(_KEY_BYTES)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    log.info("minted the at-rest encryption key at %s", path)
    return key


def encrypt(plaintext: str) -> bytes:
    """`12-byte nonce || ciphertext+tag`. A fresh random nonce per call — GCM's one hard rule is that
    a nonce is never reused under the same key, and 96 random bits is the standard way to keep that
    true without tracking a counter across restarts."""
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(_ensure_key()).encrypt(nonce, plaintext.encode(), None)


def decrypt(blob: bytes) -> str | None:
    """The plaintext, or **None** if it cannot be recovered — no keyfile (a restored backup), a blob
    written under a different key, or a tampered/truncated one. See the module docstring: an
    unrecoverable key is treated as an absent key, never as an error."""
    key = _load_key()
    if key is None:
        _warn_once(f"no at-rest encryption key at {key_path()}")
        return None
    try:
        return AESGCM(key).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], None).decode()
    except (InvalidTag, ValueError):
        _warn_once("a stored W&B key did not decrypt under this Head's key")
        return None
