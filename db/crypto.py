"""
Symmetric encryption for Monei access/refresh tokens before they touch the
database, the DB should never hold a usable token in plaintext.

Generate a key once with:
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
and set it as MONEI_TOKEN_ENCRYPTION_KEY. Losing this key means every
stored Monei connection becomes unreadable (users would need to
reconnect), treat it like any other production secret.
"""

import os

from cryptography.fernet import Fernet

_key = os.environ.get("MONEI_TOKEN_ENCRYPTION_KEY")
_fernet = Fernet(_key.encode()) if _key else None


def encrypt_token(token: str) -> str:
    if _fernet is None:
        raise RuntimeError("MONEI_TOKEN_ENCRYPTION_KEY is not set")
    return _fernet.encrypt(token.encode()).decode()


def decrypt_token(token_encrypted: str) -> str:
    if _fernet is None:
        raise RuntimeError("MONEI_TOKEN_ENCRYPTION_KEY is not set")
    return _fernet.decrypt(token_encrypted.encode()).decode()