"""
Port allocation with three-layer race condition protection:
  1. threading.Lock  — prevents concurrent threads in the same process
  2. SQLite IMMEDIATE transaction — prevents concurrent processes (DB-level lock)
  3. UNIQUE constraint on port_reservations.port — last-resort DB enforcement
"""
import socket
import sqlite3
import threading
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PortReservation

_lock = threading.Lock()


def _is_port_open(port: int) -> bool:
    """Check if a port is already bound on the host."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def _find_free_port(start: int, end: int, blocked: set[int]) -> int:
    for p in range(start, end + 1):
        if p not in blocked and not _is_port_open(p):
            return p
    raise RuntimeError(f"No free port in range {start}–{end}")


def allocate_ports(db: Session) -> tuple[int, int]:
    """
    Atomically reserve two ports (wp_port, pma_port).
    site_id is intentionally NOT set here — the Site row doesn't exist yet.
    Call link_ports_to_site() after the Site row is inserted.
    Raises RuntimeError if no ports are available.
    """
    with _lock:
        # Use a raw sqlite3 connection with isolation_level=None (autocommit)
        # so that Python never issues an implicit BEGIN and our explicit
        # BEGIN IMMEDIATE is the sole transaction — SQLAlchemy's autobegin
        # would otherwise issue its own BEGIN first, causing SQLite to ignore
        # our BEGIN IMMEDIATE and read a stale snapshot.
        conn = sqlite3.connect(str(settings.DB_PATH), isolation_level=None)
        try:
            cur = conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                rows = cur.execute(
                    "SELECT port FROM port_reservations WHERE status != 'released'"
                ).fetchall()
                db_used = {r[0] for r in rows}

                start = settings.PORT_RANGE_START
                end = settings.PORT_RANGE_END

                wp_port = _find_free_port(start, end, db_used)
                pma_port = _find_free_port(start, end, db_used | {wp_port})

                now = datetime.now(timezone.utc).isoformat()
                upsert = (
                    "INSERT INTO port_reservations (port, site_id, status, reserved_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(port) DO UPDATE SET"
                    "   site_id = NULL, status = 'reserved', reserved_at = excluded.reserved_at"
                )
                cur.execute(upsert, (wp_port, None, "reserved", now))
                cur.execute(upsert, (pma_port, None, "reserved", now))
                cur.execute("COMMIT")
            except Exception:
                try:
                    cur.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        finally:
            conn.close()

    return wp_port, pma_port


def link_ports_to_site(db: Session, wp_port: int, pma_port: int, site_id: str) -> None:
    """Update port reservations with site_id after the Site row is inserted."""
    db.query(PortReservation).filter(
        PortReservation.port.in_([wp_port, pma_port])
    ).update({"site_id": site_id})
    db.commit()


def activate_ports(db: Session, site_id: str) -> None:
    db.query(PortReservation).filter(
        PortReservation.site_id == site_id,
        PortReservation.status == "reserved",
    ).update({"status": "active"})
    db.commit()


def release_ports(db: Session, site_id: str) -> None:
    db.query(PortReservation).filter(
        PortReservation.site_id == site_id
    ).update({"status": "released"})
    db.commit()


def release_ports_by_number(db: Session, *ports: int) -> None:
    """Release specific ports directly, by port number rather than site_id.
    Needed when the Site row itself never got committed (e.g. a PK collision
    on insert) — at that point the reservations are still unlinked
    (site_id IS NULL), so release_ports()'s site_id filter would match nothing
    and the ports would stay reserved forever."""
    db.query(PortReservation).filter(
        PortReservation.port.in_(ports)
    ).update({"status": "released"}, synchronize_session=False)
    db.commit()
