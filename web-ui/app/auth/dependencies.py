from fastapi import Request, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth.service import decode_session_token
from app.models import User


def get_current_user(request: Request, db: Session) -> User:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    uid = decode_session_token(token)
    if uid is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    user = db.get(User, uid)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    return user


def require_login(request: Request, db: Session) -> User:
    """Use this in page routes — redirects to /login instead of 401."""
    token = request.cookies.get("session")
    if not token:
        return RedirectResponse(url="/login", status_code=302)
    uid = decode_session_token(token)
    if uid is None:
        return RedirectResponse(url="/login", status_code=302)
    user = db.get(User, uid)
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    return user
