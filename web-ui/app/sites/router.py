import html

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import User
from app.auth.dependencies import require_login
from app.onboarding import build_guide, uses_default_password
from app.sites import service
from app.nginx import generator as nginx_gen

router = APIRouter()
templates = Jinja2Templates(directory="templates")
# Site links (partials/site_row.html) use this, falling back to the host the
# browser used to reach EZWeb.
templates.env.globals["public_host"] = settings.PUBLIC_HOST


def _current_user(request: Request, db: Session = Depends(get_db)) -> User | RedirectResponse:
    return require_login(request, db)


def _dashboard_ctx(request: Request, user: User, db: Session, **extra) -> dict:
    sites = service.list_sites(db)
    templates_list = service.list_templates()
    return {
        "request": request,
        "user": user,
        "sites": sites,
        "templates_list": templates_list,
        "guide": build_guide(user, templates_list, sites),
        "default_pw": uses_default_password(user),
        "active_nav": "sites",
        **extra,
    }


# ── Dashboard ────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse("dashboard.html", _dashboard_ctx(request, user, db))


# ── htmx partial: first-run guide (polled while a site is building) ─────────

@router.get("/onboarding-fragment", response_class=HTMLResponse)
def onboarding_fragment(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    return templates.TemplateResponse("partials/onboarding.html", _dashboard_ctx(request, user, db))


# ── htmx partial: site list refresh ─────────────────────────────────────────

@router.get("/sites/list-fragment", response_class=HTMLResponse)
def sites_list_fragment(request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    sites = service.list_sites(db)
    return templates.TemplateResponse("partials/site_list.html", {
        "request": request,
        "user": user,
        "sites": sites,
    })


# ── htmx partial: single site status badge ───────────────────────────────────

@router.get("/sites/{site_id}/status", response_class=HTMLResponse)
def site_status(site_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    return templates.TemplateResponse("partials/status_badge.html", {
        "request": request,
        "site": site,
    })


# ── Create ────────────────────────────────────────────────────────────────────

@router.post("/sites", response_class=HTMLResponse)
def create_site(
    request: Request,
    background_tasks: BackgroundTasks,
    display_name: str = Form(...),
    template_id: str = Form(...),
    wp_title: str = Form(...),
    wp_admin_user: str = Form(...),
    wp_admin_pass: str = Form(...),
    wp_admin_email: str = Form(...),
    domain_develop: str = Form(""),
    domain_qa: str = Form(""),
    domain_prod: str = Form(""),
    stage: str = Form("develop"),
    wp_port: str = Form(""),
    pma_port: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user

    parsed_wp_port = int(wp_port) if wp_port.strip() else None
    parsed_pma_port = int(pma_port) if pma_port.strip() else None

    try:
        # The browser create-site flow has nowhere to display a one-time API
        # key (it's a form redirect, not a JSON response) — discard it here;
        # POST /api/sites/{id}/rotate-key can mint one later if this site
        # needs to be handed to an external caller (e.g. WP-Server-Dashboard).
        site, _api_key = service.prepare_site(
            db=db,
            user_id=user.id,
            display_name=display_name,
            template_id=template_id,
            wp_admin_user=wp_admin_user,
            wp_admin_pass=wp_admin_pass,
            wp_admin_email=wp_admin_email,
            wp_title=wp_title,
            domain_develop=domain_develop.strip() or None,
            domain_qa=domain_qa.strip() or None,
            domain_prod=domain_prod.strip() or None,
            stage=stage,
            wp_port=parsed_wp_port,
            pma_port=parsed_pma_port,
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        if request.headers.get("HX-Request"):
            # htmx ignores 4xx bodies by default, so answer 200 and retarget
            # the error into the still-open create modal instead.
            return HTMLResponse(
                f'<div class="alert alert-error">{html.escape(str(e))}</div>',
                headers={"HX-Retarget": "#create-error", "HX-Reswap": "innerHTML"},
            )
        return templates.TemplateResponse(
            "dashboard.html", _dashboard_ctx(request, user, db, error=str(e)), status_code=400
        )

    # docker compose runs in background — HTTP response returns immediately
    background_tasks.add_task(service.run_docker_setup, site.id, user.id)
    if request.headers.get("HX-Request"):
        # Full reload so the site list *and* the first-run guide both update.
        # (A plain 303 would make htmx swap the whole page into #sites-list.)
        return HTMLResponse("", headers={"HX-Redirect": "/?created=" + site.id})
    return RedirectResponse(url="/", status_code=303)


# ── Pause ─────────────────────────────────────────────────────────────────────

@router.post("/sites/{site_id}/pause", response_class=HTMLResponse)
async def pause_site(site_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    try:
        await service.pause_site(db, site, user.id)
    except (ValueError, RuntimeError) as e:
        pass  # status already updated to error in service
    return templates.TemplateResponse("partials/site_row.html", {
        "request": request,
        "site": db.get(type(site), site_id),
        "user": user,
    })


# ── Resume ────────────────────────────────────────────────────────────────────

@router.post("/sites/{site_id}/resume", response_class=HTMLResponse)
async def resume_site(site_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    try:
        await service.resume_site(db, site, user.id)
    except (ValueError, RuntimeError):
        pass
    return templates.TemplateResponse("partials/site_row.html", {
        "request": request,
        "site": db.get(type(site), site_id),
        "user": user,
    })


# ── Stage transition ─────────────────────────────────────────────────────────

@router.post("/sites/{site_id}/stage", response_class=HTMLResponse)
def transition_stage(site_id: str, request: Request, stage: str = Form(...), db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    try:
        service.transition_stage(db, site, stage, user.id)
    except ValueError:
        pass
    return templates.TemplateResponse("partials/site_row.html", {
        "request": request,
        "site": db.get(type(site), site_id),
        "user": user,
    })


# ── Domain edit ──────────────────────────────────────────────────────────────

@router.get("/sites/{site_id}/domains/edit", response_class=HTMLResponse)
def edit_domains_form(site_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    return templates.TemplateResponse("partials/domain_edit_form.html", {
        "request": request,
        "site": site,
    })


@router.get("/sites/{site_id}/domains/view", response_class=HTMLResponse)
def view_domains(site_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    return templates.TemplateResponse("partials/domain_cell.html", {
        "request": request,
        "site": site,
    })


@router.post("/sites/{site_id}/domains", response_class=HTMLResponse)
async def update_domains(
    site_id: str,
    request: Request,
    domain_develop: str = Form(""),
    domain_qa: str = Form(""),
    domain_prod: str = Form(""),
    db: Session = Depends(get_db),
):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    try:
        await service.update_domains(
            db, site, user.id,
            domain_develop=domain_develop.strip() or None,
            domain_qa=domain_qa.strip() or None,
            domain_prod=domain_prod.strip() or None,
        )
    except ValueError as e:
        return templates.TemplateResponse("partials/domain_edit_form.html", {
            "request": request,
            "site": site,
            "error": str(e),
            "domain_develop": domain_develop.strip(),
            "domain_qa": domain_qa.strip(),
            "domain_prod": domain_prod.strip(),
        }, status_code=400)
    except RuntimeError as e:
        # Domain metadata already committed inside update_domains() — only the
        # wp-cli siteurl/home sync failed, so show the saved state plus a warning
        # (full stdout/stderr is in the operation log via the "日誌" button).
        return templates.TemplateResponse("partials/domain_cell.html", {
            "request": request,
            "site": db.get(type(site), site_id),
            "warning": str(e),
        })
    return templates.TemplateResponse("partials/domain_cell.html", {
        "request": request,
        "site": db.get(type(site), site_id),
    })


# ── PHP 設定 ──────────────────────────────────────────────────────────────────

@router.get("/sites/{site_id}/php-config/edit", response_class=HTMLResponse)
def edit_php_config_form(site_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    return templates.TemplateResponse("partials/php_config_form.html", {
        "request": request,
        "site": site,
        "values": service.get_php_config(site),
    })


@router.post("/sites/{site_id}/php-config", response_class=HTMLResponse)
async def update_php_config(
    site_id: str,
    request: Request,
    memory_limit: str = Form(...),
    upload_max_filesize: str = Form(...),
    post_max_size: str = Form(...),
    max_execution_time: str = Form(...),
    db: Session = Depends(get_db),
):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    submitted = {
        "memory_limit": memory_limit.strip(),
        "upload_max_filesize": upload_max_filesize.strip(),
        "post_max_size": post_max_size.strip(),
        "max_execution_time": max_execution_time.strip(),
    }
    try:
        await service.update_php_config(db, site, user.id, **submitted)
    except ValueError as e:
        return templates.TemplateResponse("partials/php_config_form.html", {
            "request": request,
            "site": site,
            "values": submitted,
            "error": str(e),
        }, status_code=400)
    except RuntimeError as e:
        return templates.TemplateResponse("partials/php_config_form.html", {
            "request": request,
            "site": site,
            "values": submitted,
            "error": str(e),
        })
    return templates.TemplateResponse("partials/php_config_form.html", {
        "request": request,
        "site": site,
        "values": submitted,
        "success": "已儲存並重啟容器",
    })


# ── Delete ────────────────────────────────────────────────────────────────────

@router.delete("/sites/{site_id}", response_class=HTMLResponse)
async def delete_site(site_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    await service.delete_site(db, site, user.id)
    return HTMLResponse("")  # htmx removes the row


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.get("/sites/{site_id}/logs", response_class=HTMLResponse)
def site_logs(site_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    logs = sorted(site.logs, key=lambda l: l.created_at, reverse=True)[:20]
    return templates.TemplateResponse("partials/logs.html", {
        "request": request,
        "site": site,
        "logs": logs,
    })


# ── Nginx config ──────────────────────────────────────────────────────────────

@router.get("/sites/{site_id}/nginx-config", response_class=PlainTextResponse)
def nginx_config(site_id: str, request: Request, db: Session = Depends(get_db)):
    user = require_login(request, db)
    if isinstance(user, RedirectResponse):
        return user
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404)
    try:
        config_text = nginx_gen.generate(site)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return PlainTextResponse(
        config_text,
        headers={"Content-Disposition": f'attachment; filename="{site_id}.conf"'},
    )
