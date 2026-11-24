"""
Installed-plugin listing and activate/deactivate for a site.

Like the cleanup endpoints in service.py, the server builds the wp-cli argv
itself; the caller only supplies a plugin slug, which is validated against a
strict pattern (it must not be able to pass for a wp-cli option) and against
the site's actual plugin list.
"""
import json
import re

from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Site
from app.sites import docker_runner
from app.sites.service import _exec_wpcli, _log

# wp-cli plugin "name": directory name, or file name (without .php) for
# single-file plugins. Must start with an alphanumeric so it can never be
# read as a `--flag`.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")

# Statuses wp-cli reports that can be switched on/off. must-use plugins and
# drop-ins are loaded by WordPress unconditionally.
_TOGGLEABLE = {"active", "inactive", "active-network"}


def is_backup_plugin(slug: str) -> bool:
    """EZWeb's own backups run through All-in-One WP Migration (backups.py).
    Sites get it from a bundled fork whose directory name differs from the
    wordpress.org slug, so match loosely."""
    return "all-in-one-wp-migration" in slug.lower()


_FIELDS = "name,title,status,version,update,update_version"


def _parse_json_tail(out: str):
    """wp-cli output can be preceded by PHP notices/warnings printed by
    plugins — parse from the first '[' or '{' that yields valid JSON."""
    for i, ch in enumerate(out):
        if ch in "[{":
            try:
                return json.loads(out[i:])
            except ValueError:
                continue
    raise ValueError("無法解析 wp-cli 的輸出")


def _normalize(row: dict) -> dict:
    status = row.get("status") or ""
    name = row.get("name") or ""
    return {
        "slug": name,
        "title": row.get("title") or name,
        "status": status,
        "active": status in ("active", "active-network"),
        "toggleable": status in _TOGGLEABLE,
        "version": row.get("version") or "",
        "update_available": row.get("update") == "available",
        "update_version": row.get("update_version") or None,
        "required_by_ezweb": is_backup_plugin(name),
    }


async def list_plugins(db: Session, site: Site, user_id: int | None) -> list[dict]:
    """Raises ValueError if the site isn't running, RuntimeError if wp-cli
    failed (e.g. a plugin fatals WordPress)."""
    rc, out, err = await _exec_wpcli(
        db, site, user_id,
        ["plugin", "list", f"--fields={_FIELDS}", "--format=json"],
        "plugin-list", log=False,
    )
    if rc != 0:
        raise RuntimeError(f"讀取外掛清單失敗：\n{(err or out).strip()[-500:]}")
    try:
        rows = _parse_json_tail(out)
    except ValueError as e:
        raise RuntimeError(str(e))
    plugins = [_normalize(r) for r in rows if isinstance(r, dict)]
    # Active first, then by display name.
    plugins.sort(key=lambda p: (not p["active"], p["title"].lower()))
    return plugins


async def set_plugin_active(
    db: Session, site: Site, user_id: int | None, slug: str, active: bool
) -> tuple[dict, list[dict]]:
    """Activate/deactivate one plugin. Returns (its refreshed row, the
    refreshed full list) — callers that redraw the list reuse the second
    instead of asking wp-cli again; every wp-cli call costs seconds.

    Raises ValueError for a bad slug / unknown plugin / a plugin that can't be
    toggled, RuntimeError when wp-cli fails for another reason."""
    if not _SLUG_RE.match(slug or ""):
        raise ValueError("外掛代稱格式不正確")

    verb = "activate" if active else "deactivate"
    word = "啟用" if active else "停用"
    # wp-cli treats "already active/inactive" as success, so no pre-check is
    # needed; an unknown or must-use plugin makes it exit non-zero.
    rc, out, err = await _exec_wpcli(db, site, user_id, ["plugin", verb, slug], f"plugin-{verb}")
    detail = (err or out).strip()

    # The fresh list decides which error to report: wp-cli says "could not be
    # found" both for a slug that doesn't exist and for a must-use plugin.
    current = await list_plugins(db, site, user_id)
    plugin = next((p for p in current if p["slug"] == slug), None)
    if plugin is None:
        raise ValueError(f"這個站台沒有安裝外掛：{slug}")
    if not plugin["toggleable"]:
        raise ValueError(f"外掛「{plugin['title']}」的狀態是 {plugin['status']}，無法啟用或停用")
    if rc != 0 or plugin["active"] != active:
        raise RuntimeError(f"外掛{word}失敗：\n{detail[-500:] or '狀態沒有改變'}")
    return plugin, current


# ── Update ───────────────────────────────────────────────────────────────────

# An update downloads and unpacks a package; allow far longer than a toggle.
_UPDATE_TIMEOUT_SEC = 900
_UPDATE_ALL_TIMEOUT_SEC = 1800

_NO_PACKAGE_HINT = (
    "無法從官方來源下載這個外掛的更新。付費外掛通常需要有效授權，"
    "或改用上傳 zip 的方式更新。"
)


def _update_rows(out: str) -> list[dict]:
    """Rows of `wp plugin update --format=json`: name / old_version /
    new_version / status ("Updated" or "Error"). Empty when nothing was
    attempted (wp-cli then prints a plain message instead of JSON)."""
    try:
        rows = _parse_json_tail(out)
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    return [
        {
            "slug": r.get("name") or "",
            "old_version": r.get("old_version") or "",
            "new_version": r.get("new_version") or "",
            "updated": (r.get("status") or "").lower() == "updated",
        }
        for r in rows if isinstance(r, dict)
    ]


def _failure_reason(detail: str) -> str:
    low = detail.lower()
    if "package not available" in low or "download failed" in low or "unauthorized" in low:
        return _NO_PACKAGE_HINT
    return detail[-500:] or "wp-cli 沒有回報原因"


async def _wpcli_long(site: Site, args: list[str], timeout: int) -> tuple[int, str, str]:
    if site.status != "running":
        raise ValueError(f"站台目前狀態為 {site.status}，無法執行 wp-cli 指令")
    return await docker_runner.wp_cli_long(Path(site.site_path), *args, timeout=timeout)


async def update_plugin(db: Session, site: Site, user_id: int | None, slug: str) -> dict:
    """Updates one plugin to its latest available version.

    Returns {"plugin", "plugins", "old_version", "new_version", "updated"};
    updated is False when it was already current. Raises ValueError for a bad
    or unknown slug, RuntimeError when the update fails (the usual cause: a
    premium plugin whose package can't be downloaded)."""
    if not _SLUG_RE.match(slug or ""):
        raise ValueError("外掛代稱格式不正確")

    rc, out, err = await _wpcli_long(site, ["plugin", "update", slug, "--format=json"], _UPDATE_TIMEOUT_SEC)
    _log(db, site.id, user_id, "plugin-update", rc, out, err)
    detail = (err or out).strip()
    row = next((r for r in _update_rows(out) if r["slug"] == slug), None)

    current = await list_plugins(db, site, user_id)
    plugin = next((p for p in current if p["slug"] == slug), None)
    if plugin is None:
        raise ValueError(f"這個站台沒有安裝外掛：{slug}")

    updated = bool(row and row["updated"])
    if rc != 0 or (row and not updated) or (not updated and plugin["update_available"]):
        raise RuntimeError(f"「{plugin['title']}」更新失敗：{_failure_reason(detail)}")
    return {
        "plugin": plugin,
        "plugins": current,
        "old_version": row["old_version"] if row else plugin["version"],
        "new_version": row["new_version"] if row else plugin["version"],
        "updated": updated,
    }


async def update_all_plugins(db: Session, site: Site, user_id: int | None) -> dict:
    """Updates every plugin that has an update, in one wp-cli run. This can
    take minutes. Failures of individual plugins don't raise — they are
    reported per plugin in "results"."""
    rc, out, err = await _wpcli_long(site, ["plugin", "update", "--all", "--format=json"], _UPDATE_ALL_TIMEOUT_SEC)
    _log(db, site.id, user_id, "plugin-update-all", rc, out, err)
    results = _update_rows(out)
    if rc != 0 and not results:
        raise RuntimeError(f"外掛更新失敗：{_failure_reason((err or out).strip())}")
    current = await list_plugins(db, site, user_id)
    return {
        "results": results,
        "updated": sum(1 for r in results if r["updated"]),
        "failed": sum(1 for r in results if not r["updated"]),
        "plugins": current,
    }
