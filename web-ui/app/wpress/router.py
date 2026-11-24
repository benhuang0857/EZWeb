import os
import uuid

import aiofiles
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.requests import ClientDisconnect

from app.auth.dependencies import require_login
from app.config import settings
from app.database import get_db
from app.models import Site
from app.onboarding import uses_default_password
from app.wpress import service as svc
from app.wpress.service import TemplateError

router = APIRouter()
templates = Jinja2Templates(directory="templates")


def _usage(db: Session) -> dict[str, int]:
    rows = db.query(Site.template_id, func.count()).group_by(Site.template_id).all()
    return {tid: n for tid, n in rows}


def _list_ctx(request: Request, db: Session) -> dict:
    return {
        "request": request,
        "template_infos": svc.list_templates(_usage(db)),
        "free_space": svc.human_size(svc.free_bytes()),
        "max_upload_mb": settings.MAX_TEMPLATE_UPLOAD_MB,
    }


@router.get("/templates", response_class=HTMLResponse)
def templates_page(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    ctx = _list_ctx(request, db)
    ctx.update(user=user, default_pw=uses_default_password(user), active_nav="templates")
    return templates.TemplateResponse("templates.html", ctx)


@router.get("/templates/list-fragment", response_class=HTMLResponse)
def templates_list_fragment(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse("partials/template_list.html", _list_ctx(request, db))


def _err(msg: str, status: int) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


@router.post("/templates/upload")
async def upload_template(
    request: Request,
    name: str,
    overwrite: bool = False,
    db: Session = Depends(get_db),
):
    """Upload a .wpress file as the raw request body (not multipart).

    Streaming the body straight into a .part file next to its destination
    keeps memory flat and avoids the extra temp copy multipart parsing makes —
    templates are routinely around 1 GB. The .part is renamed into place only
    after the whole body arrived, so a half-uploaded template never shows up
    in the create-site dropdown.
    """
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return _err("登入已過期，請重新整理頁面後再登入。", 401)

    try:
        name = svc.validate_name(name)
    except TemplateError as e:
        return _err(str(e), 400)

    dest = svc.template_path(name)
    if dest.exists() and not overwrite:
        return _err(f"已經有名為「{name}」的母版。要取代它，請勾選「覆蓋同名母版」。", 409)

    try:
        length = int(request.headers.get("content-length", ""))
    except ValueError:
        return _err("缺少檔案大小資訊（Content-Length）。", 411)
    max_bytes = settings.MAX_TEMPLATE_UPLOAD_MB * 1024 * 1024
    if length > max_bytes:
        return _err(f"檔案超過上限 {settings.MAX_TEMPLATE_UPLOAD_MB} MB（可在 web-ui/.env 調整 MAX_TEMPLATE_UPLOAD_MB）。", 413)
    try:
        svc.check_space(length)
    except TemplateError as e:
        return _err(str(e), 507)

    settings.WPRESS_DIR.mkdir(parents=True, exist_ok=True)
    part = settings.WPRESS_DIR / f"{svc.PART_PREFIX}{uuid.uuid4().hex}.part"
    received = 0
    header = b""
    validated = False
    try:
        async with aiofiles.open(part, "wb") as f:
            async for chunk in request.stream():
                received += len(chunk)
                if received > length:
                    raise TemplateError("收到的資料比宣告的檔案大小還多，已中止。")
                if not validated:
                    header += chunk
                    if len(header) >= svc.WPRESS_HEADER_SIZE:
                        if not svc.looks_like_wpress(header):
                            raise TemplateError(
                                "這不是有效的 .wpress 檔。請用 All-in-One WP Migration 外掛「匯出 → 檔案」產生母版。"
                            )
                        validated = True
                        header = b""
                await f.write(chunk)
        if received != length:
            return _err("上傳沒有完成（連線中斷），請重新上傳。", 400)
        if not validated:
            return _err("檔案太小，不是有效的 .wpress 檔。", 400)
        os.replace(part, dest)
    except TemplateError as e:
        return _err(str(e), 400)
    except ClientDisconnect:
        return _err("上傳被中斷。", 400)
    finally:
        if part.exists():
            part.unlink()

    return JSONResponse({"name": name, "size": svc.human_size(received)})


@router.delete("/templates/{name}", response_class=HTMLResponse)
def delete_template(name: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    try:
        svc.delete_template(name)
    except TemplateError:
        pass  # already gone — just re-render the list
    return templates.TemplateResponse("partials/template_list.html", _list_ctx(request, db))
