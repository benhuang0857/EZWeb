from fastapi import APIRouter, Request, Form, Depends
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User
from app.auth.dependencies import require_login
from app.auth.service import verify_password, create_session_token, hash_password
from app.config import settings
from app.onboarding import DEFAULT_PASSWORD, uses_default_password

router = APIRouter()
templates = Jinja2Templates(directory="templates")


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.cookies.get("session"):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "帳號或密碼錯誤"}, status_code=401
        )
    token = create_session_token(user.id)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        "session",
        token,
        max_age=settings.SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@router.post("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("session")
    return response


# ── Change password ──────────────────────────────────────────────────────────

MIN_PASSWORD_LEN = 8


def _password_page(request: Request, user: User, error: str | None = None, status_code: int = 200):
    return templates.TemplateResponse("account_password.html", {
        "request": request,
        "user": user,
        "default_pw": uses_default_password(user),
        "min_len": MIN_PASSWORD_LEN,
        "error": error,
    }, status_code=status_code)


@router.get("/account/password", response_class=HTMLResponse)
def password_page(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return _password_page(request, user)


@router.post("/account/password", response_class=HTMLResponse)
def password_submit(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    if not verify_password(current_password, user.password_hash):
        return _password_page(request, user, "目前密碼不正確。", 400)
    if len(new_password) < MIN_PASSWORD_LEN:
        return _password_page(request, user, f"新密碼至少需要 {MIN_PASSWORD_LEN} 個字元。", 400)
    if new_password != confirm_password:
        return _password_page(request, user, "兩次輸入的新密碼不一致。", 400)
    if new_password == DEFAULT_PASSWORD or new_password == current_password:
        return _password_page(request, user, "請設定一個和目前不同、也不是預設值的密碼。", 400)
    user.password_hash = hash_password(new_password)
    db.commit()
    return RedirectResponse(url="/?password_changed=1", status_code=303)
