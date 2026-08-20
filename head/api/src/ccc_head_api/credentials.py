"""Credentials — the third-party secrets a User hands the Head, kept under that User's password.

A **Credential** is a secret the Head must reproduce verbatim inside a Spawn (a W&B API key, a GitHub
SSH key), so unlike a password it cannot be hashed. The old answer was a keyfile beside head.db
(atrest.py, which now serves only the Tide Pool key): the Head could decrypt unattended, so anything
that could read the database could read the keys. This module removes that property on purpose. **The
Head can no longer open a Credential by itself.** Everything below follows from that one decision.

Envelope encryption, one **DEK** per User, wrapped twice:

    KEK_pw  = argon2id(password, salt=users.dek_salt)          -> users.dek_wrapped_pw
    KEK_tok = HKDF-SHA256(raw session token)                   -> sessions.dek_wrapped_tok
    user_secrets.ct = AESGCM(DEK).encrypt(value)

The password wrap is the master copy; the session wraps are working copies, one per live login. What
makes the session wrap safe is a property head.db already had: `sessions` stores only sha256(token),
and the raw token lives in the client. So neither wrap can be opened from a stolen database — the
password one needs the password, the session one needs a token that isn't in there.

**Never derive a KEK from `pw_hash`.** It sits in head.db, and using it would hand the whole scheme
back to anyone holding a copy — the exact thing this exists to prevent.

What this buys, stated honestly, because the limit matters as much as the guarantee: this defends a
head.db that leaks *on its own* (a copy, a backup archive, a pulled disk). It does not defend a live
session — whoever holds one can submit a Spawn that echoes their own key back out, which is why
`/reveal` was deleted rather than gated. And a Credential exists only while its owner's password does:
an Admin password reset destroys them, because the DEK that opened them died with the old password.

Failure is uniform and quiet: an unopenable Credential reads as **absent**, never as an error. A Spawn
submitted without a Session gets no key and runs with WANDB_MODE=disabled rather than 500ing.
"""

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from argon2.low_level import Type, hash_secret_raw

_DEK_BYTES = 32     # AES-256
_SALT_BYTES = 16    # argon2's recommended salt width
_NONCE_BYTES = 12   # GCM's native nonce width; every blob here is nonce || ciphertext+tag

# argon2id parameters for the password KEK. Deliberately argon2-cffi's own defaults, the same ones
# `db._ph` already uses for the password hash itself — a login now runs two KDFs instead of one
# (measured 43.8 ms + 42.6 ms), once per 30-day session. Weakening the single KDF standing between a
# stolen database and every Credential to save 27 ms would be a bad trade. The real cost is memory:
# 64 MiB is live twice per *concurrent* login, briefly.
_TIME_COST = 3
_MEMORY_COST = 65536  # KiB
_PARALLELISM = 4

# Domain separation for the token KEK. A session token is used for two unrelated things (finding the
# session row, unwrapping the DEK) and this is what keeps those two uses from being the same value.
_TOKEN_INFO = b"ccc-credential-dek"


def new_salt() -> bytes:
    return os.urandom(_SALT_BYTES)


def new_dek() -> bytes:
    """A fresh Data Encryption Key. Minted once per User and then kept for that User's whole life —
    a password change re-wraps this same DEK, so stored ciphertexts survive it untouched."""
    return os.urandom(_DEK_BYTES)


def password_kek(password: str, salt: bytes) -> bytes:
    return hash_secret_raw(
        secret=password.encode(),
        salt=salt,
        time_cost=_TIME_COST,
        memory_cost=_MEMORY_COST,
        parallelism=_PARALLELISM,
        hash_len=_DEK_BYTES,
        type=Type.ID,
    )


def token_kek(token: str) -> bytes:
    """The KEK for a session's copy of the DEK, from the **raw** token — the one the client holds and
    head.db does not. HKDF rather than argon2 because the input is already 256 bits of entropy: there
    is nothing to stretch, and this runs on every authenticated request (measured 2.8 µs)."""
    return HKDF(algorithm=SHA256(), length=_DEK_BYTES, salt=None, info=_TOKEN_INFO).derive(
        token.encode())


def wrap(kek: bytes, dek: bytes) -> bytes:
    """`12-byte nonce || ciphertext+tag`. Fresh random nonce per call — GCM's one hard rule is never
    to reuse a nonce under the same key, and 96 random bits keeps that true without a counter that
    would have to survive restarts."""
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(kek).encrypt(nonce, dek, None)


def unwrap(kek: bytes, blob: bytes | None) -> bytes | None:
    """The DEK, or **None** if this KEK does not open it — a stale session wrap, an old password, a
    truncated blob. Never raises: see the module docstring, an unopenable Credential is an absent one."""
    if not blob:
        return None
    try:
        return AESGCM(kek).decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], None)
    except (InvalidTag, ValueError):
        return None


def seal(dek: bytes, value: str) -> bytes:
    return wrap(dek, value.encode())


def open_(dek: bytes | None, blob: bytes | None) -> str | None:
    """The Credential's plaintext, or None. `dek=None` is the ordinary no-Session case, not an error —
    it is the single line that makes every session-less caller degrade instead of fail."""
    if dek is None:
        return None
    raw = unwrap(dek, blob)
    try:
        return raw.decode() if raw is not None else None
    except UnicodeDecodeError:
        return None


# --- kinds ------------------------------------------------------------------
# A Credential's *kind* is the only thing that differs between one secret and the next, so it is a
# registry entry rather than a parallel stack of columns, routes and modules. Adding the GitHub SSH
# key is meant to be one entry here plus an input widget — nothing in db.py, main.py or the envelope
# above should have to learn a second secret exists.
#
# `hint` is the one part of a Credential stored in PLAINTEXT, and it is the whole answer to "did I
# paste the right thing?" now that the value is write-only. It has to identify without revealing, so
# it is per-kind: a W&B key is one opaque line and its last 4 characters are the natural handle; an
# SSH key will want its fingerprint instead.

def _last4(value: str) -> str:
    return value.strip()[-4:]


KINDS: dict[str, dict] = {
    "wandb": {"label": "W&B key", "hint": _last4},
}


def hint_for(kind: str, value: str) -> str:
    return KINDS[kind]["hint"](value)
