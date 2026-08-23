from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from db.models import MoneiConnection
from db.crypto import encrypt_token, decrypt_token
from connect.monei_client import refresh_access_token, expires_at_from_now


class NoMoneiConnection(Exception):
    pass


def get_valid_access_token(user_id: str, db: Session) -> str:
    """Returns a usable Monei access token for this user, refreshes it
    first if it's expired or about to expire within 60 seconds."""
    conn = db.query(MoneiConnection).filter(MoneiConnection.user_id == user_id).first()
    if not conn:
        raise NoMoneiConnection("User has not connected a Monei wallet")

    if conn.expires_at <= datetime.utcnow() + timedelta(seconds=60):
        refresh_token = decrypt_token(conn.refresh_token_encrypted)
        tokens = refresh_access_token(refresh_token)
        conn.access_token_encrypted = encrypt_token(tokens["access_token"])
        conn.refresh_token_encrypted = encrypt_token(tokens["refresh_token"])
        conn.expires_at = expires_at_from_now(tokens["expires_in"])
        db.commit()

    return decrypt_token(conn.access_token_encrypted)