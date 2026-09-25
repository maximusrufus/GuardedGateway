"""Fernet-encrypted storage for BYOK provider API keys.

Not copied from another repo — small enough to write fresh. Uses the
`cryptography` package's Fernet (AES-128-CBC + HMAC). The encryption key
(`GG_ENCRYPTION_KEY`) must be set per deployment; a fresh one is generated at
import time ONLY for local/dev/test convenience (never for a real deployment
that expects encrypted keys to survive a restart) and a loud warning is
emitted so nobody ships that default by accident.
"""

from __future__ import annotations

import os
import warnings

from cryptography.fernet import Fernet


def _get_key() -> bytes:
    env_key = os.environ.get("GG_ENCRYPTION_KEY")
    if env_key:
        return env_key.encode("utf-8")
    warnings.warn(
        "GG_ENCRYPTION_KEY is not set — generating an ephemeral key for this "
        "process only. BYOK provider keys encrypted with it will NOT be "
        "decryptable after a restart. Set GG_ENCRYPTION_KEY in production.",
        stacklevel=2,
    )
    return Fernet.generate_key()


_fernet: Fernet | None = None


def _fernet_instance() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_get_key())
    return _fernet


def reset_fernet_for_tests() -> None:
    global _fernet
    _fernet = None


def encrypt(plaintext: str) -> str:
    return _fernet_instance().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    return _fernet_instance().decrypt(ciphertext.encode("utf-8")).decode("utf-8")


def generate_key() -> str:
    """Generate a new Fernet key suitable for GG_ENCRYPTION_KEY."""
    return Fernet.generate_key().decode("utf-8")
