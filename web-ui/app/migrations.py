"""
Lightweight schema migrations for the sqlite DB — this project has no Alembic,
so schema evolution is a handful of idempotent ALTER TABLE statements run at
startup. Follows the raw-sqlite3 pattern used in app/sites/port_manager.py.
"""
import sqlite3

from app.config import settings


def run_pending_migrations() -> None:
    conn = sqlite3.connect(str(settings.DB_PATH), isolation_level=None)
    try:
        cur = conn.cursor()
        _migrate_stage_domains(cur)
        _migrate_site_api_key(cur)
        _migrate_backup_schedule(cur)
    finally:
        conn.close()


def _migrate_backup_schedule(cur: sqlite3.Cursor) -> None:
    """Per-site automatic backup settings. (The `backups` table itself is new
    and is created by Base.metadata.create_all — only columns added to an
    existing table need a migration here.)"""
    cols = {row[1] for row in cur.execute("PRAGMA table_info(sites)")}
    if "backup_schedule" in cols:
        return  # already migrated
    cur.execute("BEGIN IMMEDIATE")
    try:
        cur.execute("ALTER TABLE sites ADD COLUMN backup_schedule TEXT NOT NULL DEFAULT 'off'")
        cur.execute("ALTER TABLE sites ADD COLUMN backup_hour INTEGER NOT NULL DEFAULT 3")
        cur.execute("ALTER TABLE sites ADD COLUMN backup_keep INTEGER NOT NULL DEFAULT 5")
        cur.execute("ALTER TABLE sites ADD COLUMN backup_schedule_at DATETIME")
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise


def _migrate_stage_domains(cur: sqlite3.Cursor) -> None:
    cols = {row[1] for row in cur.execute("PRAGMA table_info(sites)")}
    if "current_stage" in cols:
        return  # already migrated
    cur.execute("BEGIN IMMEDIATE")
    try:
        cur.execute("ALTER TABLE sites ADD COLUMN current_stage TEXT NOT NULL DEFAULT 'develop'")
        cur.execute("ALTER TABLE sites ADD COLUMN domain_develop TEXT")
        cur.execute("ALTER TABLE sites ADD COLUMN domain_qa TEXT")
        cur.execute("ALTER TABLE sites ADD COLUMN domain_prod TEXT")
        # Old single-domain sites are treated as PROD (closest match to
        # the old semantics of "the one external domain").
        cur.execute(
            "UPDATE sites SET domain_prod = domain, current_stage = 'prod' "
            "WHERE domain IS NOT NULL AND TRIM(domain) != ''"
        )
        # nginx_enabled is NOT NULL with no server-side default in the old
        # schema — leaving it in place (unmapped) would make every future
        # INSERT (which no longer sets it) fail. Drop both retired columns
        # outright (requires SQLite 3.35+, DROP COLUMN).
        cur.execute("ALTER TABLE sites DROP COLUMN nginx_enabled")
        cur.execute("ALTER TABLE sites DROP COLUMN domain")
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise


def _migrate_site_api_key(cur: sqlite3.Cursor) -> None:
    cols = {row[1] for row in cur.execute("PRAGMA table_info(sites)")}
    if "api_key_hash" in cols:
        return  # already migrated
    cur.execute("BEGIN IMMEDIATE")
    try:
        cur.execute("ALTER TABLE sites ADD COLUMN api_key_hash TEXT")
        cur.execute("COMMIT")
    except Exception:
        cur.execute("ROLLBACK")
        raise
