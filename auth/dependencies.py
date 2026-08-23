from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from db.base import get_db
from db.models import User
from auth.security import decode_access_token

# Renders in Swagger's /docs "Authorize" button as a single "paste your
# token" field. (OAuth2PasswordBearer, used previously, renders the full
# OAuth2 password-grant form instead, username/password/client_id/
# client_secret, which doesn't match how /auth/login actually works
# here, a plain JSON body, not OAuth2 form-encoded fields.)
bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not credentials:
        raise unauthorized

    user_id = decode_access_token(credentials.credentials)
    if not user_id:
        raise unauthorized

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise unauthorized

    return user