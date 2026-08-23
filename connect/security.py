"""
The Monei authorize redirect is a plain browser navigation, so our
Bearer-token auth (localStorage, sent via fetch headers) never reaches the
callback, the browser just follows redirects with no custom headers.

To still know which of OUR users the callback is for, we encode the
user_id into the OAuth `state` parameter itself as a short-lived signed
JWT (reusing the same signing key as regular auth, but with a distinct
"purpose" claim so a state token can never be mistaken for, or misused
as, a real access token). Monei echoes state back unchanged on the
callback, so decoding it gives us the user_id without needing any
server-side session storage.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import jwt, JWTError

from config import JWT_SECRET_KEY, JWT_ALGORITHM

STATE_PURPOSE = "monei_oauth_state"
STATE_EXPIRE_MINUTES = 10  # matches Monei's own authorization-code expiry window


def create_oauth_state(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=STATE_EXPIRE_MINUTES)
    payload = {"sub": user_id, "purpose": STATE_PURPOSE, "exp": expire}
    return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM)


def verify_oauth_state(state: str) -> Optional[str]:
    """Returns the user_id if state is valid and unexpired, else None."""
    try:
        payload = jwt.decode(state, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return None
    if payload.get("purpose") != STATE_PURPOSE:
        return None
    return payload.get("sub")