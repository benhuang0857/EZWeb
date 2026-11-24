"""
High-level site lifecycle operations.

create_site() now has two phases:
  1. prepare_site()  — synchronous, fast: allocate ports, insert DB row, build files.
                       Called inside the HTTP request; returns immediately.
  2. _run_docker()   — async, slow: docker compose up + wp-auto-config.
                       Called as a FastAPI BackgroundTask after the response is sent.
"""
import asyncio
import hashlib
import json
import re
import secrets
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

import aiofiles
from fastapi import UploadFile
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import Site, OperationLog
from app.sites import port_manager, compose_builder, docker_runner


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return f"{slug[:40]}-{secrets.token_hex(3)}"


_CONTROL_CHAR_RE = re.compile(r"[\r\n\x00]")
_DOMAIN_RE = re.compile(
    r"^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r"(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


def _validate_no_control_chars(label: str, value: str) -> None:
    """
    These values are interpolated directly into .env / Makefile / docker-compose
    YAML via f-strings — a newline would let an attacker inject extra
    KEY=VALUE lines or YAML content, so it's rejected outright rather than escaped.
    """
    if _CONTROL_CHAR_RE.search(value):
        raise ValueError(f"{label} 不可包含換行或控制字元。")


def _validate_domain(domain: str) -> None:
    if not _DOMAIN_RE.match(domain):
        raise ValueError(f"域名格式不正確：{domain}")


def _log(db: Session, site_id: str, user_id: int | None, op: str, rc: int, out: str, err: str):
    result = "success" if rc == 0 else "failure"
    db.add(OperationLog(
        site_id=site_id, user_id=user_id, operation=op,
        result=result, stdout=out[-4000:], stderr=err[-4000:],
    ))
    db.commit()


def _update_status(db: Session, site: Site, status: str, error_msg: str | None = None):
    site.status = status
    site.error_msg = error_msg
    db.commit()


# ── Rate limit ────────────────────────────────────────────────────────────────

class RateLimitError(Exception):
    """Raised by check_destructive_rate_limit — kept distinct from ValueError/
    RuntimeError so routers map it to 429 instead of 400/502."""


def check_destructive_rate_limit(db: Session, site_id: str, operations: tuple[str, ...]) -> None:
    """Bounds how often a site's destructive endpoints (cleanup execute, plugin
    zip install) can run, so a single leaked/misused per-site key can't be used
    to fire unlimited destructive operations. No new dependency/table: reuses
    OperationLog, which every such operation already writes to via _log().

    This is a soft guard against key misuse, not a hard security boundary —
    a concurrent race right at the threshold could let one or two extra calls
    through under load, which is acceptable here.
    """
    cutoff = datetime.utcnow() - timedelta(seconds=settings.DESTRUCTIVE_OP_WINDOW_SEC)
    count = (
        db.query(OperationLog)
        .filter(
            OperationLog.site_id == site_id,
            OperationLog.operation.in_(operations),
            OperationLog.created_at >= cutoff,
        )
        .count()
    )
    if count >= settings.DESTRUCTIVE_OP_MAX:
        window_min = settings.DESTRUCTIVE_OP_WINDOW_SEC // 60
        raise RateLimitError(
            f"操作過於頻繁，請稍後再試（每 {window_min} 分鐘最多 {settings.DESTRUCTIVE_OP_MAX} 次）"
        )


# ── List / Get ────────────────────────────────────────────────────────────────

def list_sites(db: Session) -> list[Site]:
    return db.query(Site).order_by(Site.created_at.desc()).all()


def get_site(db: Session, site_id: str) -> Site | None:
    return db.get(Site, site_id)


def list_templates() -> list[str]:
    return sorted(p.stem for p in settings.WPRESS_DIR.glob("*.wpress"))


# ── Create (Phase 1 — fast, inside HTTP request) ──────────────────────────────

def prepare_site(
    db: Session,
    user_id: int,
    display_name: str,
    template_id: str,
    wp_admin_user: str,
    wp_admin_pass: str,
    wp_admin_email: str,
    wp_title: str,
    domain_develop: str | None = None,
    domain_qa: str | None = None,
    domain_prod: str | None = None,
    stage: str = "develop",
    wp_port: int | None = None,
    pma_port: int | None = None,
) -> tuple[Site, str]:
    """
    Allocate ports, create Site row, build file structure.
    Does NOT run docker — caller must schedule _run_docker() as a background task.
    Raises ValueError / FileNotFoundError on validation failure.

    Returns (site, plaintext_api_key) — the key is shown once here and never
    persisted in plaintext; only its hash is stored on the Site row. Callers
    that don't need it (e.g. the browser create-site form) should discard it.
    """
    for label, value in (
        ("站台名稱", display_name),
        ("網站標題", wp_title),
        ("管理員帳號", wp_admin_user),
        ("管理員密碼", wp_admin_pass),
        ("管理員 Email", wp_admin_email),
    ):
        _validate_no_control_chars(label, value)

    if template_id not in list_templates():
        raise ValueError(f"未知的模板：{template_id}")

    if stage not in ("develop", "qa", "prod"):
        raise ValueError(f"未知的階段：{stage}")

    stage_domains = {"develop": domain_develop, "qa": domain_qa, "prod": domain_prod}
    for domain in stage_domains.values():
        if domain:
            _validate_domain(domain)

    site_id = _slug(display_name)
    db_password = secrets.token_urlsafe(16)
    plaintext_api_key = secrets.token_urlsafe(32)

    # Port allocation (race-condition-safe)
    if wp_port or pma_port:
        _validate_user_ports(db, wp_port, pma_port)
        allocated_wp, allocated_pma = wp_port, pma_port
    else:
        allocated_wp, allocated_pma = port_manager.allocate_ports(db)

    site_path = settings.SITES_DIR / site_id

    site = Site(
        id=site_id,
        display_name=display_name,
        owner_id=user_id,
        status="creating",
        wp_port=allocated_wp,
        pma_port=allocated_pma,
        template_id=template_id,
        wp_admin_user=wp_admin_user,
        wp_admin_pass=wp_admin_pass,
        wp_admin_email=wp_admin_email,
        wp_title=wp_title,
        db_password=db_password,
        api_key_hash=hashlib.sha256(plaintext_api_key.encode()).hexdigest(),
        current_stage=stage,
        domain_develop=domain_develop,
        domain_qa=domain_qa,
        domain_prod=domain_prod,
        site_path=str(site_path),
    )
    db.add(site)
    try:
        db.commit()
    except Exception:
        # e.g. PK collision if _slug()'s random suffix repeats — extremely rare,
        # but without this the ports allocated above would stay reserved forever.
        # The reservations aren't linked to a site_id yet, so release by port
        # number rather than release_ports(site_id).
        db.rollback()
        port_manager.release_ports_by_number(db, allocated_wp, allocated_pma)
        raise
    port_manager.link_ports_to_site(db, allocated_wp, allocated_pma, site_id)

    # Build file structure (fast, local I/O only)
    try:
        compose_builder.build_site_files(
            site_id=site_id,
            wp_port=allocated_wp,
            pma_port=allocated_pma,
            template_id=template_id,
            wp_title=wp_title,
            wp_admin_user=wp_admin_user,
            wp_admin_pass=wp_admin_pass,
            wp_admin_email=wp_admin_email,
            db_password=db_password,
            domain=stage_domains[stage],
        )
    except Exception as e:
        _update_status(db, site, "error", str(e))
        port_manager.release_ports(db, site_id)
        raise

    return site, plaintext_api_key


# ── Create (Phase 2 — slow, background task) ──────────────────────────────────

def run_docker_setup(site_id: str, user_id: int) -> None:
    """
    Background task: runs docker compose up + wp-auto-config.
    Creates its own DB session (the request session is already closed).
    """
    asyncio.run(_run_docker(site_id, user_id))


async def _run_docker(site_id: str, user_id: int) -> None:
    db = SessionLocal()
    try:
        site = db.get(Site, site_id)
        if not site:
            return
        site_path = Path(site.site_path)

        # docker compose up -d --build
        rc, out, err = await docker_runner.compose_up(site_path)
        _log(db, site_id, user_id, "create-up", rc, out, err)
        if rc != 0:
            _update_status(db, site, "error", err[-500:])
            port_manager.release_ports(db, site_id)
            return

        # wp-auto-config (healthcheck + wpcli restore)
        rc, out, err = await docker_runner.compose_auto_config(site_path)
        _log(db, site_id, user_id, "create-autoconfig", rc, out, err)
        if rc != 0:
            _update_status(db, site, "error", err[-500:])
            return

        _update_status(db, site, "running")
        port_manager.activate_ports(db, site_id)
    except Exception as e:
        db.rollback()
        site = db.get(Site, site_id)
        if site:
            _update_status(db, site, "error", str(e)[:500])
    finally:
        db.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _validate_user_ports(db: Session, wp_port: int | None, pma_port: int | None):
    from app.models import PortReservation
    if wp_port is None or pma_port is None:
        raise ValueError("手動指定 port 時，wp_port 和 pma_port 都必須填寫。")
    if wp_port == pma_port:
        raise ValueError("wp_port 和 pma_port 不能相同。")
    existing = {r.port for r in db.query(PortReservation).filter(PortReservation.status != "released").all()}
    for p in (wp_port, pma_port):
        if p in existing:
            raise ValueError(f"Port {p} 已被使用。")
    db.add(PortReservation(port=wp_port, site_id=None, status="reserved"))
    db.add(PortReservation(port=pma_port, site_id=None, status="reserved"))
    db.commit()


# ── API key ───────────────────────────────────────────────────────────────────

def rotate_site_api_key(db: Session, site: Site, user_id: int | None) -> str:
    """Generates a fresh per-site API key, stores only its SHA-256 hash, and
    returns the plaintext once. Also the backfill path for sites created
    before per-site keys existed (api_key_hash starts out NULL for those)."""
    new_key = secrets.token_urlsafe(32)
    site.api_key_hash = hashlib.sha256(new_key.encode()).hexdigest()
    db.commit()
    _log(db, site.id, user_id, "rotate-api-key", 0, "", "")  # never log key material
    return new_key


# ── Pause ─────────────────────────────────────────────────────────────────────

async def pause_site(db: Session, site: Site, user_id: int) -> None:
    if site.status != "running":
        raise ValueError("只有運行中的站台才能暫停。")
    _update_status(db, site, "paused")
    rc, out, err = await docker_runner.compose_stop(Path(site.site_path))
    _log(db, site.id, user_id, "pause", rc, out, err)
    if rc != 0:
        _update_status(db, site, "error", err[-500:])
        raise RuntimeError(f"docker compose stop failed:\n{err}")


# ── Stage transition ─────────────────────────────────────────────────────────

def transition_stage(db: Session, site: Site, new_stage: str, user_id: int) -> None:
    """
    Pure metadata update — no docker/wp-cli/nginx side effects. All three
    stage domains, if bound, already proxy to the same wp_port; this only
    changes which stage the site is labeled as.
    """
    if new_stage not in ("develop", "qa", "prod"):
        raise ValueError(f"未知的階段：{new_stage}")
    old_stage = site.current_stage
    site.current_stage = new_stage
    db.commit()
    _log(db, site.id, user_id, "stage-transition", 0, "", f"{old_stage} -> {new_stage}")


# ── Domain edit ──────────────────────────────────────────────────────────────

async def update_domains(
    db: Session,
    site: Site,
    user_id: int,
    domain_develop: str | None,
    domain_qa: str | None,
    domain_prod: str | None,
) -> None:
    """
    Updates the three stage domains (metadata, same as transition_stage()).
    If this changes the domain bound to the site's *current* stage, also
    pushes the new value to WordPress's siteurl/home via wp-cli so the live
    site's canonical URL tracks what's actually labeled as active — otherwise
    they silently drift apart (see the redirect-to-old-domain issue this was
    added to fix). The other two (inactive) stage domains only affect nginx
    config generation, same as before.
    """
    for domain in (domain_develop, domain_qa, domain_prod):
        if domain:
            _validate_domain(domain)

    new_by_stage = {"develop": domain_develop, "qa": domain_qa, "prod": domain_prod}
    old_by_stage = {"develop": site.domain_develop, "qa": site.domain_qa, "prod": site.domain_prod}
    stage = site.current_stage
    active_changed = new_by_stage.get(stage) != old_by_stage.get(stage)

    site.domain_develop = domain_develop
    site.domain_qa = domain_qa
    site.domain_prod = domain_prod
    db.commit()
    _log(db, site.id, user_id, "domains-update", 0, "",
         f"develop={domain_develop or '-'} qa={domain_qa or '-'} prod={domain_prod or '-'}")

    if active_changed:
        active_domain = new_by_stage[stage]
        url = f"http://{active_domain}" if active_domain else f"http://localhost:{site.wp_port}"
        site_dir = Path(site.site_path)
        rc1, out1, err1 = await docker_runner.wp_cli(site_dir, "option", "update", "siteurl", url)
        rc2, out2, err2 = await docker_runner.wp_cli(site_dir, "option", "update", "home", url)
        rc = rc1 or rc2
        _log(db, site.id, user_id, "siteurl-sync", rc, out1 + out2, err1 + err2)
        if rc != 0:
            raise RuntimeError(f"域名已儲存，但同步 WordPress siteurl 失敗：\n{(err1 or err2)[-500:]}")


# ── PHP 設定（config/php.conf.ini）───────────────────────────────────────────
#
# 這個檔案透過 bind mount 進 `wordpress` 和 `wpcli` 兩個服務的
# /usr/local/etc/php/conf.d/php.ini（docker-compose.yml），所以兩邊會吃到同一份
# 設定——調高 memory_limit 之類的值同時解決「網站本身跑不動」跟「wp-cli 指令
# 因為記憶體不足直接 fatal error」這兩個問題。PHP 只在程序啟動時讀 php.ini，
# 改檔案本身不會生效，寫完一定要 restart 這兩個 container。

_PHP_CONFIG_FIELDS = {
    # key -> (label, regex 驗證, 錯誤訊息)
    "memory_limit": ("Memory Limit", re.compile(r"^\d+[KMG]$", re.I), "格式錯誤，例如 512M、2048M"),
    "upload_max_filesize": ("Upload Max Filesize", re.compile(r"^\d+[KMG]$", re.I), "格式錯誤，例如 512M、2048M"),
    "post_max_size": ("Post Max Size", re.compile(r"^\d+[KMG]$", re.I), "格式錯誤，例如 512M、2048M"),
    "max_execution_time": ("Max Execution Time (秒)", re.compile(r"^\d+$"), "必須是整數"),
}


def get_php_config(site: Site) -> dict:
    """讀取 site 的 config/php.conf.ini，回傳 {key: value}。缺值的欄位回傳空字串。"""
    path = Path(site.site_path) / "config" / "php.conf.ini"
    values = {key: "" for key in _PHP_CONFIG_FIELDS}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key in values:
            values[key] = val.strip()
    return values


async def update_php_config(db: Session, site: Site, user_id: int | None, **fields: str) -> None:
    """驗證後整份改寫 config/php.conf.ini，然後 restart wordpress + wpcli 讓新值生效。"""
    for key, value in fields.items():
        if key not in _PHP_CONFIG_FIELDS:
            raise ValueError(f"未知的設定項目：{key}")
        _, pattern, hint = _PHP_CONFIG_FIELDS[key]
        if not pattern.match(value):
            label = _PHP_CONFIG_FIELDS[key][0]
            raise ValueError(f"{label}：{hint}")

    site_dir = Path(site.site_path)
    ini_path = site_dir / "config" / "php.conf.ini"
    lines = ["file_uploads = On"]
    for key in _PHP_CONFIG_FIELDS:
        lines.append(f"{key} = {fields[key]}")
    ini_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    rc, out, err = await docker_runner.compose_restart(site_dir, "wordpress", "wpcli")
    _log(db, site.id, user_id, "php-config-update", rc, out, err)
    if rc != 0:
        raise RuntimeError(f"設定已寫入，但重啟容器失敗：\n{err[-500:]}")


# ── WP-CLI passthrough ──────────────────────────────────────────────────────

async def _exec_wpcli(
    db: Session, site: Site, user_id: int | None, args: list[str], op: str
) -> tuple[int, str, str]:
    """Shared "check running -> run -> log" sequence behind run_wpcli() and
    every cleanup/health function below, so they all log under meaningful
    per-operation names instead of the generic wpcli:<verb> run_wpcli uses."""
    if site.status != "running":
        raise ValueError(f"站台目前狀態為 {site.status}，無法執行 wp-cli 指令")
    site_dir = Path(site.site_path)
    rc, out, err = await docker_runner.wp_cli(site_dir, *args)
    _log(db, site.id, user_id, op, rc, out, err)
    return rc, out, err


async def run_wpcli(db: Session, site: Site, user_id: int, args: list[str]) -> tuple[int, str, str]:
    """Caller (app/api/router.py) is responsible for the whitelist check —
    this just runs it against the site's wpcli container and logs the result."""
    return await _exec_wpcli(db, site, user_id, args, f"wpcli:{args[0]}")


# ── Cleanup ───────────────────────────────────────────────────────────────────
# These endpoints are a layer above the raw /wpcli passthrough: the server
# builds a fixed, safe wp-cli command for each category itself, the caller
# only picks *which* category — it never gets to supply a verb — so this
# deliberately bypasses app/api/wpcli_whitelist.py's check_wpcli_args (that
# whitelist exists to bound the raw passthrough where the caller DOES supply
# an argv). Never uses raw `db query`, matching that whitelist's rationale.

_CLEANUP_CATEGORIES = ("revisions", "auto_drafts", "spam_comments", "expired_transients")


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


async def _count_expired_transients(db: Session, site: Site, user_id: int | None) -> int:
    """`wp transient` has no `list` subcommand — the only way to enumerate
    transients is via their backing options (`_transient_timeout_*`), whose
    values are unix timestamps compared client-side against now()."""
    rc, out, _ = await _exec_wpcli(
        db, site, user_id,
        ["option", "list", "--search=_transient_timeout_*", "--format=json"],
        "cleanup-scan-transients",
    )
    if rc != 0 or not out.strip():
        return 0
    try:
        rows = json.loads(out)
    except ValueError:
        return 0
    now = time.time()
    expired = 0
    for row in rows:
        try:
            if float(row.get("option_value", 0)) < now:
                expired += 1
        except (TypeError, ValueError):
            continue
    return expired


async def scan_cleanup(db: Session, site: Site, user_id: int | None) -> dict[str, int]:
    """Read-only counts — no rate limit needed."""
    _, revisions_out, _ = await _exec_wpcli(
        db, site, user_id, ["post", "list", "--post_type=revision", "--format=ids"], "cleanup-scan-revisions"
    )
    _, drafts_out, _ = await _exec_wpcli(
        db, site, user_id, ["post", "list", "--post_status=auto-draft", "--format=ids"], "cleanup-scan-drafts"
    )
    _, spam_out, _ = await _exec_wpcli(
        db, site, user_id, ["comment", "list", "--status=spam", "--format=ids"], "cleanup-scan-spam"
    )
    return {
        "revisions": len(revisions_out.split()),
        "auto_drafts": len(drafts_out.split()),
        "spam_comments": len(spam_out.split()),
        "expired_transients": await _count_expired_transients(db, site, user_id),
    }


async def _delete_posts_by_query(
    db: Session, site: Site, user_id: int | None, query_flag: str, op: str
) -> int:
    _, out, _ = await _exec_wpcli(db, site, user_id, ["post", "list", query_flag, "--format=ids"], f"{op}-list")
    ids = out.split()
    for chunk in _chunks(ids, 200):
        await _exec_wpcli(db, site, user_id, ["post", "delete", *chunk, "--force"], op)
    return len(ids)


async def _delete_spam_comments(db: Session, site: Site, user_id: int | None) -> int:
    _, out, _ = await _exec_wpcli(
        db, site, user_id, ["comment", "list", "--status=spam", "--format=ids"], "cleanup-exec-spam-list"
    )
    ids = out.split()
    for chunk in _chunks(ids, 200):
        await _exec_wpcli(db, site, user_id, ["comment", "delete", *chunk, "--force"], "cleanup-exec-spam")
    return len(ids)


async def execute_cleanup(
    db: Session, site: Site, user_id: int | None, categories: list[str] | None
) -> dict[str, int | None]:
    check_destructive_rate_limit(db, site.id, ("cleanup-execute",))

    categories = categories or list(_CLEANUP_CATEGORIES)
    unknown = set(categories) - set(_CLEANUP_CATEGORIES)
    if unknown:
        raise ValueError(f"未知的清理類別：{', '.join(sorted(unknown))}")

    # Top-level marker row, separate from the per-category detail rows below,
    # so check_destructive_rate_limit's ("cleanup-execute",) filter counts
    # exactly one row per call regardless of how many categories were requested.
    _log(db, site.id, user_id, "cleanup-execute", 0, f"categories={categories}", "")

    deleted: dict[str, int | None] = {}
    if "revisions" in categories:
        deleted["revisions"] = await _delete_posts_by_query(
            db, site, user_id, "--post_type=revision", "cleanup-exec-revisions"
        )
    if "auto_drafts" in categories:
        deleted["auto_drafts"] = await _delete_posts_by_query(
            db, site, user_id, "--post_status=auto-draft", "cleanup-exec-drafts"
        )
    if "spam_comments" in categories:
        deleted["spam_comments"] = await _delete_spam_comments(db, site, user_id)
    if "expired_transients" in categories:
        rc, _, _ = await _exec_wpcli(db, site, user_id, ["transient", "delete-expired"], "cleanup-exec-transients")
        deleted["expired_transients"] = None if rc != 0 else 0  # wp-cli doesn't report a count for this one
    return deleted


# ── Health ────────────────────────────────────────────────────────────────────

async def check_health(db: Session, site: Site, user_id: int | None) -> dict:
    """Read-only — no rate limit needed."""
    installed_rc, _, _ = await _exec_wpcli(db, site, user_id, ["core", "is-installed"], "health-core")
    checksums_rc, _, _ = await _exec_wpcli(db, site, user_id, ["core", "verify-checksums"], "health-checksums")
    _, plugin_out, _ = await _exec_wpcli(
        db, site, user_id, ["plugin", "list", "--update=available", "--format=count"], "health-plugin-updates"
    )
    _, theme_out, _ = await _exec_wpcli(
        db, site, user_id, ["theme", "list", "--update=available", "--format=count"], "health-theme-updates"
    )
    return {
        "site_status": site.status,
        "wp_installed": installed_rc == 0,
        "checksums_ok": checksums_rc == 0,
        "plugin_updates_available": int(plugin_out.strip() or 0),
        "theme_updates_available": int(theme_out.strip() or 0),
    }


# ── Plugin zip install ──────────────────────────────────────────────────────

async def install_plugin_zip(db: Session, site: Site, user_id: int | None, file: UploadFile) -> dict:
    """Copies an uploaded .zip into the site's WORDPRESS_DATA_DIR volume — the
    only filesystem path the `wordpress` and `wpcli` containers share, since
    there's no host bind mount for wp-content (see docker-compose.yml) — then
    installs it via wp-cli. There is no other API path to get a plugin zip
    into a docker-managed site today; the raw /wpcli passthrough can only
    install from a repo slug or a path already inside the container.

    The destination sits under wp-content/uploads/, which is directly
    web-servable by the wordpress container's Apache for as long as the file
    sits there. There's no other shared path available given the current
    compose file, so this is an accepted, documented trade-off — mitigated by
    an unguessable random filename and always deleting the file (host +
    container copies) in `finally`, minimizing the exposure window.
    """
    if site.status != "running":
        raise ValueError(f"站台目前狀態為 {site.status}，無法安裝外掛")

    check_destructive_rate_limit(db, site.id, ("plugin-install-zip",))

    site_dir = Path(site.site_path)
    token = secrets.token_hex(16)
    host_tmp_dir = site_dir / "uploads-tmp"
    host_tmp_dir.mkdir(exist_ok=True)
    host_tmp_path = host_tmp_dir / f"{token}.zip"
    container_tmp_rel = f"wp-content/uploads/.ezweb-tmp/{token}.zip"
    container_tmp_abs = f"/var/www/html/{container_tmp_rel}"

    max_bytes = settings.MAX_PLUGIN_ZIP_MB * 1024 * 1024
    try:
        written = 0
        async with aiofiles.open(host_tmp_path, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(f"檔案超過大小上限（{settings.MAX_PLUGIN_ZIP_MB}MB）")
                await f.write(chunk)

        mkdir_rc, _, mkdir_err = await docker_runner.compose_exec(
            site_dir, "wordpress", "mkdir", "-p", "/var/www/html/wp-content/uploads/.ezweb-tmp"
        )
        if mkdir_rc != 0:
            raise RuntimeError(f"建立暫存目錄失敗：\n{mkdir_err}")

        cp_rc, _, cp_err = await docker_runner.compose_cp_to(site_dir, host_tmp_path, "wordpress", container_tmp_abs)
        if cp_rc != 0:
            raise RuntimeError(f"上傳檔案至容器失敗：\n{cp_err}")

        rc, out, err = await _exec_wpcli(
            db, site, user_id, ["plugin", "install", container_tmp_abs, "--activate"], "plugin-install-zip"
        )
        if rc != 0:
            raise RuntimeError(f"外掛安裝失敗：\n{err}")
        return {"success": True, "output": out}
    finally:
        host_tmp_path.unlink(missing_ok=True)
        await docker_runner.compose_exec(site_dir, "wordpress", "rm", "-f", container_tmp_abs)


# ── Resume ────────────────────────────────────────────────────────────────────

async def resume_site(db: Session, site: Site, user_id: int) -> None:
    if site.status != "paused":
        raise ValueError("只有已暫停的站台才能恢復。")
    _update_status(db, site, "running")
    rc, out, err = await docker_runner.compose_start(Path(site.site_path))
    _log(db, site.id, user_id, "resume", rc, out, err)
    if rc != 0:
        _update_status(db, site, "error", err[-500:])
        raise RuntimeError(f"docker compose start failed:\n{err}")


# ── Delete ────────────────────────────────────────────────────────────────────

def _env_value(site_dir: Path, key: str) -> str | None:
    try:
        for line in (site_dir / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip() or None
    except OSError:
        pass
    return None


async def _remove_site_dir(site_dir: Path) -> str | None:
    """Delete a site directory. Returns None on success, else a description
    of what could not be removed.

    On Linux, MariaDB writes sites/<id>/mysql/* as the container's mysql user,
    so the EZWeb process can't delete those files itself. When a plain delete
    leaves anything behind, a throwaway root container empties the directory
    instead. It runs an image that is already local (the site's own MariaDB
    image, else the shared wpcli image) with --pull never, so deleting a site
    never downloads anything.
    """
    if not site_dir.exists():
        return None

    # A root container is about to `rm -rf` whatever is mounted: refuse
    # anything that isn't exactly one directory below SITES_DIR.
    sites_root = settings.SITES_DIR.resolve()
    if site_dir.is_symlink():  # check before resolve(), which follows links
        return f"拒絕刪除：{site_dir} 是符號連結"
    target = site_dir.resolve()
    if target.parent != sites_root:
        return f"拒絕刪除：{site_dir} 不在站台目錄 {sites_root} 底下"

    shutil.rmtree(target, ignore_errors=True)
    if not target.exists():
        return None

    images = []
    mariadb_version = _env_value(target, "MARIADB_VERSION")
    if mariadb_version:
        images.append(f"mariadb:{mariadb_version}")
    images.append("wpcli")

    last_err = ""
    for image in images:
        rc, _out, err = await docker_runner.run_docker(
            "run", "--rm", "--pull", "never", "--network", "none",
            "--user", "0:0", "--entrypoint", "find",
            "-v", f"{target}:/target",
            image,
            "/target", "-mindepth", "1", "-delete",
        )
        if rc == 0:
            break
        last_err = err.strip()[-300:]

    shutil.rmtree(target, ignore_errors=True)
    if target.exists():
        return f"無法完全刪除 {target}（可手動執行 sudo rm -rf {target}）。{last_err}"
    return None


async def delete_site(db: Session, site: Site, user_id: int) -> None:
    _update_status(db, site, "deleting")
    site_path = Path(site.site_path)

    rc, out, err = await docker_runner.compose_down(site_path)
    _log(db, site.id, user_id, "delete", rc, out, err)

    leftover = await _remove_site_dir(site_path)
    if leftover:
        # The site is still removed from EZWeb — but say so loudly in the log
        # rather than silently leaking disk space like before.
        _log(db, site.id, user_id, "delete-cleanup", 1, "", leftover)

    port_manager.release_ports(db, site.id)
    db.delete(site)
    db.commit()
