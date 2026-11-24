"""JSON API for machine-to-machine callers (e.g. an external WP site),
authenticated via API key instead of the browser cookie session used by
app/sites/router.py.

Two auth tiers (see app/auth/api_key.py):
  - require_api_key      — fleet-admin (global key): list/create/delete sites,
                            rotate a site's key.
  - require_site_api_key — that site's own key: everything else. A caller
                            holding only a site's key can never reach another
                            site or the fleet-admin actions above.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.schemas import (
    BackupSchedule, CleanupExecuteRequest, DomainsUpdate, SiteCreate, StageUpdate, WpCliCommand,
)
from app.api.wpcli_whitelist import check_wpcli_args
from app.auth.api_key import require_api_key, require_site_api_key
from app.database import get_db
from app.models import Site, User
from app.sites import backups, health, plugins, service

router = APIRouter(prefix="/api/sites", tags=["api"])


def _serialize(site: Site) -> dict:
    return {
        "id": site.id,
        "display_name": site.display_name,
        "status": site.status,
        "wp_port": site.wp_port,
        "pma_port": site.pma_port,
        "template_id": site.template_id,
        "current_stage": site.current_stage,
        "domain_develop": site.domain_develop,
        "domain_qa": site.domain_qa,
        "domain_prod": site.domain_prod,
        "error_msg": site.error_msg,
        "created_at": site.created_at.isoformat(),
        "updated_at": site.updated_at.isoformat(),
    }


def _get_or_404(db: Session, site_id: str) -> Site:
    site = service.get_site(db, site_id)
    if not site:
        raise HTTPException(404, "Site not found")
    return site


# ── Fleet-admin endpoints (global key) ──────────────────────────────────────

@router.get("", dependencies=[Depends(require_api_key)])
def list_sites(db: Session = Depends(get_db)):
    return [_serialize(s) for s in service.list_sites(db)]


@router.post("", status_code=201)
def create_site(
    body: SiteCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_key),
):
    try:
        site, api_key = service.prepare_site(
            db=db,
            user_id=user.id,
            display_name=body.display_name,
            template_id=body.template_id,
            wp_admin_user=body.wp_admin_user,
            wp_admin_pass=body.wp_admin_pass,
            wp_admin_email=body.wp_admin_email,
            wp_title=body.wp_title,
            domain_develop=body.domain_develop,
            domain_qa=body.domain_qa,
            domain_prod=body.domain_prod,
            stage=body.stage,
            wp_port=body.wp_port,
            pma_port=body.pma_port,
        )
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    background_tasks.add_task(service.run_docker_setup, site.id, user.id)
    # api_key is shown here once — it is never stored in plaintext and cannot
    # be recovered later, only rotated (POST /{site_id}/rotate-key).
    return {**_serialize(site), "api_key": api_key}


@router.delete("/{site_id}", status_code=204)
async def delete_site(
    site_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_key),
):
    site = _get_or_404(db, site_id)
    await service.delete_site(db, site, user.id)


@router.post("/{site_id}/rotate-key")
def rotate_site_api_key(
    site_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(require_api_key),
):
    """Issues a fresh per-site API key (fleet-admin only — minting/replacing a
    credential is credential management, not a site operation). This is also
    the backfill path for sites that existed before per-site keys did: their
    api_key_hash is NULL until this is called once."""
    site = _get_or_404(db, site_id)
    key = service.rotate_site_api_key(db, site, user.id)
    return {"api_key": key}


# ── Site-scoped endpoints (that site's own key) ─────────────────────────────

@router.get("/{site_id}")
def get_site(site: Site = Depends(require_site_api_key)):
    return _serialize(site)


@router.post("/{site_id}/pause")
async def pause_site(site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    try:
        await service.pause_site(db, site, site.owner_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return _serialize(site)


@router.post("/{site_id}/resume")
async def resume_site(site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    try:
        await service.resume_site(db, site, site.owner_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return _serialize(site)


@router.post("/{site_id}/stage")
def transition_stage(
    body: StageUpdate,
    site: Site = Depends(require_site_api_key),
    db: Session = Depends(get_db),
):
    try:
        service.transition_stage(db, site, body.stage, site.owner_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _serialize(site)


@router.post("/{site_id}/wpcli")
async def wpcli_passthrough(
    body: WpCliCommand,
    site: Site = Depends(require_site_api_key),
    db: Session = Depends(get_db),
):
    """Structured wp-cli passthrough for external managers (e.g. WP-Server-
    Dashboard). Args are forwarded as a real argv list to `docker compose exec
    wpcli wp ...` (see docker_runner.wp_cli) — never assembled into a shell
    string — so there's no shell-metacharacter injection surface here. The
    whitelist in app/api/wpcli_whitelist.py only bounds *scope* (blocks eval /
    db query), it is not a defense against injection."""
    reason = check_wpcli_args(body.args)
    if reason:
        raise HTTPException(400, reason)
    try:
        rc, out, err = await service.run_wpcli(db, site, site.owner_id, body.args)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"return_code": rc, "output": out, "error": err}


@router.get("/{site_id}/cleanup/scan")
async def cleanup_scan(site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    try:
        return await service.scan_cleanup(db, site, site.owner_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/{site_id}/cleanup/execute")
async def cleanup_execute(
    body: CleanupExecuteRequest,
    site: Site = Depends(require_site_api_key),
    db: Session = Depends(get_db),
):
    try:
        return await service.execute_cleanup(db, site, site.owner_id, body.categories)
    except service.RateLimitError as e:
        raise HTTPException(429, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.get("/{site_id}/health")
async def site_health(site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    try:
        return await health.check_health(db, site, site.owner_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ── Plugins ──────────────────────────────────────────────────────────────────

@router.get("/{site_id}/plugins")
async def list_plugins(site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    """Installed plugins with their status, version and available update."""
    try:
        return await plugins.list_plugins(db, site, site.owner_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


async def _set_plugin_active(db: Session, site: Site, slug: str, active: bool) -> dict:
    try:
        plugin, _all = await plugins.set_plugin_active(db, site, site.owner_id, slug, active)
        return plugin
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.post("/{site_id}/plugins/{slug}/activate")
async def activate_plugin(slug: str, site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    return await _set_plugin_active(db, site, slug, True)


@router.post("/{site_id}/plugins/{slug}/deactivate")
async def deactivate_plugin(slug: str, site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    return await _set_plugin_active(db, site, slug, False)


@router.post("/{site_id}/plugins/update-all")
async def update_all_plugins(site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    """Updates every plugin that has an update available. Synchronous and
    slow (minutes on a site with many plugins) — use a generous client
    timeout. Plugins that fail (typically premium ones without a license) are
    listed in "results" with updated=false; the call itself still succeeds."""
    try:
        result = await plugins.update_all_plugins(db, site, site.owner_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return {k: result[k] for k in ("results", "updated", "failed")}


@router.post("/{site_id}/plugins/{slug}/update")
async def update_plugin(slug: str, site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    try:
        result = await plugins.update_plugin(db, site, site.owner_id, slug)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return {k: result[k] for k in ("plugin", "old_version", "new_version", "updated")}


# ── Backups (All-in-One WP Migration exports) ───────────────────────────────

def _backup_or_404(db: Session, site: Site, backup_id: int):
    backup = backups.get_backup(db, site, backup_id)
    if not backup:
        raise HTTPException(404, "Backup not found")
    return backup


@router.get("/{site_id}/backups")
def list_backups(site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    return [backups.serialize(b) for b in backups.list_backups(db, site)]


@router.post("/{site_id}/backups", status_code=202)
def create_backup(
    background_tasks: BackgroundTasks,
    site: Site = Depends(require_site_api_key),
    db: Session = Depends(get_db),
):
    """Starts an export and returns immediately with status "running" — poll
    GET /{site_id}/backups/{backup_id} until it is "success" or "failed"."""
    try:
        backup = backups.start_backup(db, site, trigger="manual")
    except backups.BackupError as e:
        raise HTTPException(409, str(e))
    background_tasks.add_task(backups.run_backup_task, backup.id)
    return backups.serialize(backup)


@router.get("/{site_id}/backups/schedule")
def get_backup_schedule(site: Site = Depends(require_site_api_key)):
    return backups.schedule_info(site)


@router.put("/{site_id}/backups/schedule")
def set_backup_schedule(
    body: BackupSchedule,
    site: Site = Depends(require_site_api_key),
    db: Session = Depends(get_db),
):
    try:
        backups.update_schedule(db, site, body.schedule, body.hour, body.keep)
    except backups.BackupError as e:
        raise HTTPException(400, str(e))
    return backups.schedule_info(site)


@router.get("/{site_id}/backups/{backup_id}")
def get_backup(backup_id: int, site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    return backups.serialize(_backup_or_404(db, site, backup_id))


@router.get("/{site_id}/backups/{backup_id}/download")
def download_backup(backup_id: int, site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    backup = _backup_or_404(db, site, backup_id)
    path = backups.backup_file(backup)
    if path is None:
        raise HTTPException(404, "這個備份沒有可下載的檔案")
    return FileResponse(path, filename=backup.filename, media_type="application/octet-stream")


@router.delete("/{site_id}/backups/{backup_id}", status_code=204)
def delete_backup(backup_id: int, site: Site = Depends(require_site_api_key), db: Session = Depends(get_db)):
    backup = _backup_or_404(db, site, backup_id)
    try:
        backups.delete_backup(db, backup)
    except backups.BackupError as e:
        raise HTTPException(409, str(e))


@router.post("/{site_id}/plugins/install-zip")
async def install_plugin_zip(
    file: UploadFile = File(...),
    site: Site = Depends(require_site_api_key),
    db: Session = Depends(get_db),
):
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "僅接受 .zip 檔案")
    try:
        return await service.install_plugin_zip(db, site, site.owner_id, file)
    except service.RateLimitError as e:
        raise HTTPException(429, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@router.post("/{site_id}/domains")
async def update_domains(
    body: DomainsUpdate,
    site: Site = Depends(require_site_api_key),
    db: Session = Depends(get_db),
):
    try:
        await service.update_domains(
            db, site, site.owner_id,
            domain_develop=body.domain_develop,
            domain_qa=body.domain_qa,
            domain_prod=body.domain_prod,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    return _serialize(site)
