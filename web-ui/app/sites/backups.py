"""
Site backups: All-in-One WP Migration exports (.wpress), manual or scheduled.

A backup runs `wp ai1wm backup` inside the site's wpcli container. The plugin
writes the archive into wp-content/ai1wm-backups/ in the site's Docker volume;
it is then copied out to the host (settings.BACKUPS_DIR/<site_id>/) and removed
from the volume, so backups don't grow the volume and survive the site's
containers. A .wpress backup is the same format as a site template, so one can
be saved as a template ("母版") to clone the site.

Backups take minutes, so they run outside the HTTP request: a `backups` row is
created first (status=running) and updated when the work finishes. Manual
backups run as a FastAPI BackgroundTask; scheduled ones are started by
scheduler_loop(), a single asyncio task started at app startup.
"""
import asyncio
import re
import shutil
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Backup, Site
from app.sites import docker_runner
from app.sites.service import _log
from app.wpress import service as wpress
from app.wpress.service import human_size

CONTAINER_DIR = "/var/www/html/wp-content/ai1wm-backups"
SCHEDULES = ("off", "daily", "weekly")
SCHEDULER_INTERVAL_SEC = 300
MAX_KEEP = 50

# File names come from `ls` inside the container and are passed on to
# `docker compose cp` / `rm` as single argv entries. Reject anything that
# could be a path or an option.
_SAFE_NAME_RE = re.compile(r"^[^/\\\x00-\x1f-][^/\\\x00-\x1f]*\.wpress$")


class BackupError(ValueError):
    pass


# ── Paths / queries ──────────────────────────────────────────────────────────

def site_backup_dir(site_id: str) -> Path:
    return settings.BACKUPS_DIR / site_id


def backup_file(backup: Backup) -> Path | None:
    """Host path of a finished backup, or None if it has no file (still
    running, failed, or the file was removed by hand)."""
    if not backup.filename:
        return None
    p = site_backup_dir(backup.site_id) / backup.filename
    return p if p.is_file() else None


def list_backups(db: Session, site: Site, limit: int = 100) -> list[Backup]:
    return (
        db.query(Backup)
        .filter(Backup.site_id == site.id)
        .order_by(Backup.created_at.desc(), Backup.id.desc())
        .limit(limit)
        .all()
    )


def get_backup(db: Session, site: Site, backup_id: int) -> Backup | None:
    b = db.get(Backup, backup_id)
    return b if b and b.site_id == site.id else None


def running_backup(db: Session, site: Site) -> Backup | None:
    return (
        db.query(Backup)
        .filter(Backup.site_id == site.id, Backup.status == "running")
        .first()
    )


def serialize(b: Backup) -> dict:
    return {
        "id": b.id,
        "site_id": b.site_id,
        "status": b.status,
        "trigger": b.trigger,
        "filename": b.filename,
        "size": b.size,
        "error": b.error,
        "created_at": b.created_at.isoformat(),
        "finished_at": b.finished_at.isoformat() if b.finished_at else None,
    }


# ── Start (fast, inside the HTTP request) ────────────────────────────────────

def start_backup(db: Session, site: Site, trigger: str = "manual") -> Backup:
    """Validates and records a new backup as running. The caller must then
    schedule run_backup_task(backup.id) (or await run_backup())."""
    if site.status != "running":
        raise BackupError(f"站台目前狀態為 {site.status}，必須在運行中才能備份。")
    if running_backup(db, site):
        raise BackupError("這個站台已經有一個備份正在進行。")

    free = shutil.disk_usage(settings.BACKUPS_DIR).free
    need = settings.BACKUP_MIN_FREE_MB * 1024 * 1024
    if free < need:
        raise BackupError(
            f"備份磁碟空間不足：可用 {human_size(free)}，至少需要 {human_size(need)}"
            f"（可在 web-ui/.env 調整 BACKUP_MIN_FREE_MB 或 BACKUP_DIR）。"
        )

    backup = Backup(site_id=site.id, status="running", trigger=trigger)
    db.add(backup)
    db.commit()
    return backup


# ── Run (slow, background) ───────────────────────────────────────────────────

def run_backup_task(backup_id: int) -> None:
    """BackgroundTask entry point (runs in a worker thread)."""
    asyncio.run(run_backup(backup_id))


async def _container_backups(site_dir: Path) -> set[str]:
    rc, out, _ = await docker_runner.compose_exec(site_dir, "wpcli", "ls", "-1", CONTAINER_DIR)
    if rc != 0:
        return set()
    return {line.strip() for line in out.splitlines() if line.strip().endswith(".wpress")}


async def _export(site: Site, dest_dir: Path, stamp: str) -> tuple[str, int]:
    """Runs the export and moves the archive to the host.
    Returns (host filename, size). Raises BackupError."""
    site_dir = Path(site.site_path)

    # The backups directory is created by root during site setup, but wp-cli
    # runs as the web server user and must be able to write into it.
    rc, out, err = await docker_runner.compose_exec_as(
        site_dir, "wpcli", "0:0", "sh", "-c",
        f'mkdir -p {CONTAINER_DIR} && chown -R "$(stat -c %u:%g /var/www/html/wp-content)" {CONTAINER_DIR}',
    )
    if rc != 0:
        raise BackupError(f"無法準備備份目錄：{(err or out).strip()[-300:]}")

    before = await _container_backups(site_dir)
    rc, out, err = await docker_runner.wp_cli_long(
        site_dir, "ai1wm", "backup", timeout=settings.BACKUP_TIMEOUT_SEC
    )
    created = sorted(await _container_backups(site_dir) - before)

    if rc != 0 or not created:
        msg = (err or out).strip()[-600:] or "沒有產生備份檔"
        if "not a registered wp command" in msg:
            msg = "站台上的 All-in-One WP Migration 外掛沒有啟用，請先在「外掛」區塊啟用它。"
        # A failed export can still leave a partial archive behind.
        for name in created:
            if _SAFE_NAME_RE.match(name):
                await docker_runner.compose_exec(site_dir, "wpcli", "rm", "-f", f"{CONTAINER_DIR}/{name}")
        raise BackupError(f"匯出失敗：{msg}")

    name = created[-1]
    if not _SAFE_NAME_RE.match(name):
        raise BackupError(f"備份檔名不符合預期：{name!r}")
    container_path = f"{CONTAINER_DIR}/{name}"

    filename = f"{site.id}-{stamp}.wpress"
    dest = dest_dir / filename
    part = dest_dir / f".{filename}.part"
    try:
        rc, out, err = await docker_runner.compose_cp_from(
            site_dir, "wpcli", container_path, part, timeout=settings.BACKUP_TIMEOUT_SEC
        )
        if rc != 0 or not part.is_file():
            raise BackupError(f"無法把備份檔複製到主機：{(err or out).strip()[-300:]}")
        size = part.stat().st_size
        if size == 0:
            raise BackupError("匯出的備份檔是空的。")
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
        await docker_runner.compose_exec(site_dir, "wpcli", "rm", "-f", container_path)
    return filename, size


async def run_backup(backup_id: int) -> None:
    """Performs a backup previously recorded by start_backup(). Uses its own
    DB session — the request's session is closed by the time this runs."""
    db = SessionLocal()
    try:
        backup = db.get(Backup, backup_id)
        if not backup or backup.status != "running":
            return
        site = db.get(Site, backup.site_id)
        if not site:
            return

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            dest_dir = site_backup_dir(site.id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            filename, size = await _export(site, dest_dir, stamp)
        except Exception as e:
            if not isinstance(e, BackupError):
                traceback.print_exc()
            backup.status = "failed"
            backup.error = str(e)[:1000]
            backup.finished_at = datetime.utcnow()
            db.commit()
            _log(db, site.id, None, f"backup-{backup.trigger}", 1, "", str(e))
            return

        backup.status = "success"
        backup.filename = filename
        backup.size = size
        backup.finished_at = datetime.utcnow()
        db.commit()
        _log(db, site.id, None, f"backup-{backup.trigger}", 0, f"{filename} ({human_size(size)})", "")

        if backup.trigger == "auto":
            prune_auto_backups(db, site)
    finally:
        db.close()


def fail_interrupted_backups() -> int:
    """Called at startup: a backup still marked running belonged to the
    previous process and will never finish."""
    db = SessionLocal()
    try:
        stale = db.query(Backup).filter(Backup.status == "running").all()
        for b in stale:
            b.status = "failed"
            b.error = "EZWeb 在備份進行中重新啟動，這次備份已中斷。"
            b.finished_at = datetime.utcnow()
        db.commit()
        return len(stale)
    finally:
        db.close()


# ── Delete / retention ───────────────────────────────────────────────────────

def _remove_file(backup: Backup) -> None:
    if backup.filename:
        (site_backup_dir(backup.site_id) / backup.filename).unlink(missing_ok=True)


def delete_backup(db: Session, backup: Backup) -> None:
    if backup.status == "running":
        raise BackupError("備份還在進行中，無法刪除。")
    _remove_file(backup)
    db.delete(backup)
    db.commit()


def prune_auto_backups(db: Session, site: Site) -> int:
    """Keeps the newest `backup_keep` successful automatic backups. Manual
    backups are never pruned — someone asked for those explicitly."""
    keep = max(1, site.backup_keep or 1)
    autos = (
        db.query(Backup)
        .filter(Backup.site_id == site.id, Backup.trigger == "auto", Backup.status == "success")
        .order_by(Backup.created_at.desc(), Backup.id.desc())
        .all()
    )
    for old in autos[keep:]:
        _remove_file(old)
        db.delete(old)
    db.commit()
    return max(0, len(autos) - keep)


def delete_site_backups(site_id: str) -> None:
    """Removes a deleted site's backup files (its rows cascade with the site)."""
    d = site_backup_dir(site_id)
    if d.parent == settings.BACKUPS_DIR and d.is_dir() and not d.is_symlink():
        shutil.rmtree(d, ignore_errors=True)


# ── Save as template ─────────────────────────────────────────────────────────

async def save_as_template(backup: Backup, name: str) -> str:
    """Copies a finished backup into the template directory so new sites can
    be created from it. Returns the template name."""
    src = backup_file(backup)
    if backup.status != "success" or src is None:
        raise BackupError("這個備份沒有可用的檔案。")
    try:
        name = wpress.validate_name(name)
        dest = wpress.template_path(name)
        if dest.exists():
            raise BackupError(f"已經有名為「{name}」的母版，請換一個名稱。")
        wpress.check_space(src.stat().st_size)
    except wpress.TemplateError as e:
        raise BackupError(str(e))

    settings.WPRESS_DIR.mkdir(parents=True, exist_ok=True)
    part = settings.WPRESS_DIR / f"{wpress.PART_PREFIX}{backup.id}-{name}.part"
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, shutil.copyfile, src, part)
        part.replace(dest)
    finally:
        part.unlink(missing_ok=True)
    return name


# ── Schedule ─────────────────────────────────────────────────────────────────

def update_schedule(db: Session, site: Site, schedule: str, hour: int, keep: int) -> None:
    if schedule not in SCHEDULES:
        raise BackupError(f"未知的排程：{schedule}")
    if not 0 <= hour <= 23:
        raise BackupError("執行時間必須是 0 到 23 之間的整點。")
    if not 1 <= keep <= MAX_KEEP:
        raise BackupError(f"保留份數必須在 1 到 {MAX_KEEP} 之間。")
    site.backup_schedule = schedule
    site.backup_hour = hour
    site.backup_keep = keep
    site.backup_schedule_at = datetime.utcnow()
    db.commit()
    _log(db, site.id, None, "backup-schedule", 0, f"schedule={schedule} hour={hour} keep={keep}", "")
    if schedule != "off":
        prune_auto_backups(db, site)


def schedule_info(site: Site) -> dict:
    return {
        "schedule": site.backup_schedule,
        "hour": site.backup_hour,
        "keep": site.backup_keep,
    }


def _latest_slot_utc(hour: int, now_local: datetime | None = None) -> datetime:
    """The most recent occurrence of `hour`:00 server-local time, as naive UTC
    (how every timestamp in the DB is stored)."""
    now_local = now_local or datetime.now().astimezone()
    slot = now_local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot > now_local:
        slot -= timedelta(days=1)
    return slot.astimezone(timezone.utc).replace(tzinfo=None)


def last_auto_attempt(db: Session, site: Site) -> datetime | None:
    """Start time of the newest automatic backup, successful or not. Failures
    count too, so a site that can't be backed up is retried once per slot
    rather than every scheduler tick."""
    b = (
        db.query(Backup)
        .filter(Backup.site_id == site.id, Backup.trigger == "auto")
        .order_by(Backup.created_at.desc())
        .first()
    )
    return b.created_at if b else None


def is_due(site: Site, last_auto: datetime | None, now_local: datetime | None = None) -> bool:
    if site.backup_schedule not in ("daily", "weekly"):
        return False
    slot = _latest_slot_utc(site.backup_hour, now_local)
    if last_auto is None:
        # Never ran: wait for the first slot after the schedule was saved.
        return (site.backup_schedule_at or site.created_at) < slot
    if site.backup_schedule == "daily":
        return last_auto < slot
    # weekly: the slot must be the 7th day after the last run (6 days leaves
    # room for the minutes the previous run started after its own slot).
    return last_auto < slot - timedelta(days=6)


def next_run_local(db: Session, site: Site) -> datetime | None:
    """When the next automatic backup is expected (server-local, aware)."""
    if site.backup_schedule not in ("daily", "weekly"):
        return None
    now_local = datetime.now().astimezone()
    last_auto = last_auto_attempt(db, site)
    for day in range(0, 9):
        probe = (now_local + timedelta(days=day)).replace(
            hour=site.backup_hour, minute=0, second=0, microsecond=0
        )
        if probe < now_local:
            continue
        if is_due(site, last_auto, probe + timedelta(seconds=1)):
            return probe
    return None


async def _tick() -> None:
    db = SessionLocal()
    try:
        sites = (
            db.query(Site)
            .filter(Site.backup_schedule.in_(("daily", "weekly")), Site.status == "running")
            .all()
        )
        due = [s.id for s in sites if is_due(s, last_auto_attempt(db, s))]
    finally:
        db.close()

    # One site at a time: an export is heavy on disk and CPU.
    for site_id in due:
        db = SessionLocal()
        try:
            site = db.get(Site, site_id)
            if not site or site.status != "running":
                continue
            if running_backup(db, site):
                continue  # a manual backup is in progress — try again next tick
            try:
                backup_id = start_backup(db, site, trigger="auto").id
            except BackupError as e:
                # Record it, so the failure is visible and this slot isn't
                # retried every few minutes.
                db.add(Backup(
                    site_id=site.id, status="failed", trigger="auto",
                    error=str(e)[:1000], finished_at=datetime.utcnow(),
                ))
                db.commit()
                continue
        finally:
            db.close()
        await run_backup(backup_id)


async def scheduler_loop() -> None:
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
        await asyncio.sleep(SCHEDULER_INTERVAL_SEC)
