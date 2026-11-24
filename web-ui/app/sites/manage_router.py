"""
Browser UI for managing one site: /sites/{site_id} and the htmx partials it
loads (plugins, health check, backups). Cookie-session auth, like
app/sites/router.py; the JSON equivalents live in app/api/router.py.

Each section is its own partial fetched after the page loads, because every
one of them has to run wp-cli inside the site's container (seconds, not
milliseconds) and one slow or broken section shouldn't hold up the others.
"""
import shutil
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth.dependencies import require_login
from app.config import settings
from app.database import get_db
from app.models import Backup, Site, User
from app.onboarding import uses_default_password
from app.sites import backups, health, plugins, service
from app.wpress.service import human_size

router = APIRouter()
templates = Jinja2Templates(directory="templates")
templates.env.globals["public_host"] = settings.PUBLIC_HOST


def _localtime(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """DB timestamps are naive UTC; show them in the server's local time."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime(fmt)


templates.env.filters["localtime"] = _localtime
templates.env.filters["human_size"] = lambda n: human_size(n or 0)


class _Redirect(Exception):
    def __init__(self, response: RedirectResponse):
        self.response = response


def _auth(request: Request, db: Session, site_id: str) -> tuple[User, Site]:
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        raise _Redirect(user)
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    return user, site


def _login_redirect(request: Request, exc: _Redirect):
    # An htmx partial must not swap the login page into a card — make the
    # browser navigate instead.
    if request.headers.get("HX-Request"):
        return HTMLResponse("", headers={"HX-Redirect": "/login"})
    return exc.response


# ── Page ─────────────────────────────────────────────────────────────────────

@router.get("/sites/{site_id}", response_class=HTMLResponse)
def site_detail(site_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        user, site = _auth(request, db, site_id)
    except _Redirect as r:
        return _login_redirect(request, r)
    return templates.TemplateResponse("site_detail.html", {
        "request": request,
        "user": user,
        "site": site,
        "default_pw": uses_default_password(user),
        "active_nav": "sites",
        **_backups_ctx(db, site),
    })


# ── Plugins ──────────────────────────────────────────────────────────────────

def _last_backup(db: Session, site: Site) -> datetime | None:
    """Shown in the update confirmation, so updating without a fresh backup
    is an informed decision."""
    b = (
        db.query(Backup)
        .filter(Backup.site_id == site.id, Backup.status == "success")
        .order_by(Backup.created_at.desc())
        .first()
    )
    return b.created_at if b else None


async def _plugins_response(request: Request, db: Session, site: Site, user: User, **extra):
    ctx = {"request": request, "site": site, "plugins": None, "last_backup": _last_backup(db, site), **extra}
    if ctx["plugins"] is None:
        try:
            ctx["plugins"] = await plugins.list_plugins(db, site, user.id)
        except (ValueError, RuntimeError) as e:
            ctx.setdefault("load_error", str(e))
    return templates.TemplateResponse("partials/site_plugins.html", ctx)


@router.get("/sites/{site_id}/plugins", response_class=HTMLResponse)
async def plugins_fragment(site_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        user, site = _auth(request, db, site_id)
    except _Redirect as r:
        return _login_redirect(request, r)
    return await _plugins_response(request, db, site, user)


@router.post("/sites/{site_id}/plugins/{slug}/{action}", response_class=HTMLResponse)
async def plugin_action(site_id: str, slug: str, action: str, request: Request, db: Session = Depends(get_db)):
    """activate / deactivate / update. `update?json=1` answers with JSON
    instead of the re-rendered list: the page's "全部更新" runs the updates
    one by one and shows progress itself."""
    as_json = action == "update" and bool(request.query_params.get("json"))
    try:
        user, site = _auth(request, db, site_id)
    except _Redirect as r:
        if as_json:
            return JSONResponse({"ok": False, "message": "登入已過期，請重新整理頁面後再登入。"}, status_code=401)
        return _login_redirect(request, r)
    if action not in ("activate", "deactivate", "update"):
        raise HTTPException(404)

    extra = {}
    try:
        if action == "update":
            result = await plugins.update_plugin(db, site, user.id, slug)
            extra["plugins"] = result["plugins"]
            title = result["plugin"]["title"]
            if result["updated"]:
                extra["notice"] = f"已將「{title}」從 {result['old_version']} 更新到 {result['new_version']}。"
            else:
                extra["notice"] = f"「{title}」已經是最新版本。"
        else:
            p, extra["plugins"] = await plugins.set_plugin_active(db, site, user.id, slug, action == "activate")
            extra["notice"] = f"已{'啟用' if p['active'] else '停用'}「{p['title']}」。"
    except (ValueError, RuntimeError) as e:
        extra["error"] = str(e)

    if as_json:
        return JSONResponse({"ok": "error" not in extra, "message": extra.get("error") or extra.get("notice")})
    return await _plugins_response(request, db, site, user, **extra)


# ── Health ───────────────────────────────────────────────────────────────────

@router.get("/sites/{site_id}/health", response_class=HTMLResponse)
async def health_fragment(site_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        user, site = _auth(request, db, site_id)
    except _Redirect as r:
        return _login_redirect(request, r)
    ctx = {"request": request, "site": site, "report": None}
    try:
        ctx["report"] = await health.check_health(db, site, user.id)
        ctx["checked_at"] = datetime.now().strftime("%H:%M:%S")
    except ValueError as e:
        ctx["load_error"] = str(e)
    return templates.TemplateResponse("partials/site_health.html", ctx)


# ── Backups ──────────────────────────────────────────────────────────────────

def _backups_ctx(db: Session, site: Site, **extra) -> dict:
    items = backups.list_backups(db, site, limit=30)
    return {
        "site": site,
        "backups": [(b, backups.backup_file(b) is not None) for b in items],
        "backup_running": any(b.status == "running" for b in items),
        "next_run": backups.next_run_local(db, site),
        "backup_free": human_size(_free_backup_space()),
        "backup_dir": str(backups.site_backup_dir(site.id)),
        **extra,
    }


def _free_backup_space() -> int:
    return shutil.disk_usage(settings.BACKUPS_DIR).free


def _backups_response(request: Request, db: Session, site: Site, **extra):
    return templates.TemplateResponse(
        "partials/site_backups.html", {"request": request, **_backups_ctx(db, site, **extra)}
    )


@router.get("/sites/{site_id}/backups", response_class=HTMLResponse)
def backups_fragment(site_id: str, request: Request, db: Session = Depends(get_db)):
    try:
        _user, site = _auth(request, db, site_id)
    except _Redirect as r:
        return _login_redirect(request, r)
    return _backups_response(request, db, site)


@router.post("/sites/{site_id}/backups", response_class=HTMLResponse)
def backup_start(
    site_id: str, request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)
):
    try:
        _user, site = _auth(request, db, site_id)
    except _Redirect as r:
        return _login_redirect(request, r)
    try:
        backup = backups.start_backup(db, site, trigger="manual")
    except backups.BackupError as e:
        return _backups_response(request, db, site, error=str(e))
    background_tasks.add_task(backups.run_backup_task, backup.id)
    return _backups_response(request, db, site, notice="備份已開始，完成後會出現在下方清單。")


@router.post("/sites/{site_id}/backups/schedule", response_class=HTMLResponse)
def backup_schedule(
    site_id: str,
    request: Request,
    schedule: str = Form(...),
    hour: int = Form(3),
    keep: int = Form(5),
    db: Session = Depends(get_db),
):
    try:
        _user, site = _auth(request, db, site_id)
    except _Redirect as r:
        return _login_redirect(request, r)
    try:
        backups.update_schedule(db, site, schedule, hour, keep)
    except backups.BackupError as e:
        return _backups_response(request, db, site, error=str(e))
    return _backups_response(request, db, site, notice="自動備份設定已儲存。")


@router.get("/sites/{site_id}/backups/{backup_id}/download")
def backup_download(site_id: str, backup_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        _user, site = _auth(request, db, site_id)
    except _Redirect as r:
        return _login_redirect(request, r)
    backup = backups.get_backup(db, site, backup_id)
    path = backups.backup_file(backup) if backup else None
    if path is None:
        raise HTTPException(404, "找不到備份檔")
    return FileResponse(path, filename=backup.filename, media_type="application/octet-stream")


@router.delete("/sites/{site_id}/backups/{backup_id}", response_class=HTMLResponse)
def backup_delete(site_id: str, backup_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        _user, site = _auth(request, db, site_id)
    except _Redirect as r:
        return _login_redirect(request, r)
    backup = backups.get_backup(db, site, backup_id)
    if backup:
        try:
            backups.delete_backup(db, backup)
        except backups.BackupError as e:
            return _backups_response(request, db, site, error=str(e))
    return _backups_response(request, db, site)


@router.post("/sites/{site_id}/backups/{backup_id}/save-template", response_class=HTMLResponse)
async def backup_save_template(
    site_id: str,
    backup_id: int,
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        _user, site = _auth(request, db, site_id)
    except _Redirect as r:
        return _login_redirect(request, r)
    backup = backups.get_backup(db, site, backup_id)
    if not backup:
        raise HTTPException(404)
    try:
        saved = await backups.save_as_template(backup, name)
    except backups.BackupError as e:
        return _backups_response(request, db, site, error=str(e))
    return _backups_response(
        request, db, site, notice=f"已存成母版「{saved}」，新增站台時可以選用。"
    )
