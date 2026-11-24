"""
Site health check: a set of measurements, each with a value and a traffic
light (ok / warn / fail, or info for values that have no good/bad reading).

Everything here is read-only. The probes are independent, so they run
concurrently, and the number of wp-cli calls is kept to a minimum: each one
boots WordPress inside the container, which takes seconds on a real site.

Most WordPress-side numbers come from a single `wp eval` of a fixed PHP
snippet (_PROBE_PHP). The caller never supplies code, so this is not the
arbitrary-eval the /wpcli whitelist blocks.
"""
import asyncio
import json
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Backup, Site
from app.sites import docker_runner
from app.sites.service import _exec_wpcli
from app.wpress.service import human_size

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
_SEVERITY = {INFO: 0, OK: 0, WARN: 1, FAIL: 2}

_MARKER = "EZWEB_HEALTH_JSON:"

# The update counts read the same transients wp-admin's "Updates" screen uses.
# wp_update_plugins()/wp_update_themes()/wp_version_check() refresh them, but
# throttle themselves (they return at once if checked recently), so this only
# reaches wordpress.org when the data is stale.
_PROBE_PHP = r"""
global $wpdb;
require_once ABSPATH . "wp-admin/includes/update.php";
wp_version_check();
wp_update_plugins();
wp_update_themes();
$plugins = get_site_transient("update_plugins");
$themes = get_site_transient("update_themes");
$core_new = "";
foreach ((array) get_core_updates() as $u) {
    if (isset($u->response) && $u->response === "upgrade") { $core_new = $u->version; break; }
}
$autoload = $wpdb->get_var("SELECT SUM(LENGTH(option_value)) FROM {$wpdb->options} WHERE autoload IN ('yes','on','auto','auto-on')");
$dbsize = $wpdb->get_var($wpdb->prepare("SELECT SUM(data_length + index_length) FROM information_schema.TABLES WHERE table_schema = %s", DB_NAME));
echo "EZWEB_HEALTH_JSON:" . json_encode(array(
    "wp_version" => get_bloginfo("version"),
    "core_update" => $core_new,
    "plugin_updates" => empty($plugins->response) ? 0 : count($plugins->response),
    "theme_updates" => empty($themes->response) ? 0 : count($themes->response),
    "home" => home_url(),
    "db_bytes" => (int) $dbsize,
    "autoload_bytes" => (int) $autoload,
    "wp_debug" => defined("WP_DEBUG") && WP_DEBUG,
    "wp_debug_display" => defined("WP_DEBUG_DISPLAY") && WP_DEBUG_DISPLAY,
    "blog_public" => (int) get_option("blog_public"),
    "active_plugins" => count((array) get_option("active_plugins")),
));
"""

# PHP facts must come from the `wordpress` container — that is what serves the
# site. The wpcli container is a different image with its own PHP build.
_PHP_PROBE = 'echo PHP_VERSION, "|", ini_get("memory_limit");'

# Thresholds. Autoload follows WordPress's own Site Health limit (800 KB).
_HTTP_SLOW_SEC, _HTTP_VERY_SLOW_SEC = 1.0, 3.0
_AUTOLOAD_WARN, _AUTOLOAD_FAIL = 800 * 1024, 2 * 1024 * 1024
_DB_WARN, _DB_FAIL = 1024 ** 3, 5 * 1024 ** 3
_DISK_WARN_PCT, _DISK_FAIL_PCT = 20, 10
_BACKUP_WARN_DAYS, _BACKUP_FAIL_DAYS = 7, 30
_REQUIRED_SERVICES = ("wordpress", "mysql")


def _check(key: str, label: str, status: str, value: str, detail: str = "") -> dict:
    return {"key": key, "label": label, "status": status, "value": value, "detail": detail}


# ── Individual probes ────────────────────────────────────────────────────────

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # report the 3xx itself instead of following it


def _http_probe_blocking(port: int, host_header: str | None) -> tuple[int | None, float, str]:
    """GET the site's front page through its published port on this host.
    Returns (status_code or None, seconds, error)."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}/", headers={"User-Agent": "EZWeb-HealthCheck"})
    if host_header:
        # WordPress redirects any other host name to its configured URL;
        # asking for the configured one gets the real page.
        req.add_header("Host", host_header)
    opener = urllib.request.build_opener(_NoRedirect)
    start = time.monotonic()
    try:
        with opener.open(req, timeout=15) as resp:
            resp.read(1024)
            return resp.status, time.monotonic() - start, ""
    except urllib.error.HTTPError as e:
        return e.code, time.monotonic() - start, ""
    except Exception as e:  # connection refused, timeout, reset…
        return None, time.monotonic() - start, str(getattr(e, "reason", e))


async def _http_check(site: Site, home: str | None) -> dict:
    host_header = urlsplit(home).netloc if home else None
    loop = asyncio.get_running_loop()
    code, secs, err = await loop.run_in_executor(None, _http_probe_blocking, site.wp_port, host_header)
    ms = f"{secs * 1000:.0f} ms"
    label = "網站回應"
    if code is None:
        return _check("http", label, FAIL, "無法連線", f"連不上 port {site.wp_port}：{err}")
    if code >= 500:
        return _check("http", label, FAIL, f"HTTP {code}", f"伺服器錯誤，回應時間 {ms}")
    if code >= 400:
        return _check("http", label, WARN, f"HTTP {code} · {ms}",
                      "首頁回傳錯誤碼。網站本身有回應，請檢查首頁設定（設定 → 閱讀）。")
    if secs >= _HTTP_VERY_SLOW_SEC:
        return _check("http", label, FAIL, f"HTTP {code} · {ms}", f"回應時間超過 {_HTTP_VERY_SLOW_SEC:.0f} 秒")
    if secs >= _HTTP_SLOW_SEC:
        return _check("http", label, WARN, f"HTTP {code} · {ms}", f"回應時間超過 {_HTTP_SLOW_SEC:.0f} 秒")
    return _check("http", label, OK, f"HTTP {code} · {ms}")


def _parse_ps(out: str) -> dict[str, str]:
    """{service: state} from `docker compose ps --format json` (one object
    per line, or a single JSON array on older Compose)."""
    out = out.strip()
    rows: list = []
    if out.startswith("["):
        try:
            rows = json.loads(out)
        except ValueError:
            rows = []
    else:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    return {r.get("Service", ""): (r.get("State") or "").lower() for r in rows if isinstance(r, dict)}


def _containers_check(rc: int, out: str, err: str) -> dict:
    label = "容器狀態"
    if rc != 0:
        return _check("containers", label, FAIL, "無法查詢", (err or out).strip()[-300:])
    states = _parse_ps(out)
    down = [s for s in _REQUIRED_SERVICES if states.get(s) != "running"]
    if down:
        return _check("containers", label, FAIL, f"{'、'.join(down)} 未運行",
                      "；".join(f"{s}: {states.get(s, '不存在')}" for s in down))
    extra_down = [s for s in ("phpmyadmin", "wpcli") if states.get(s) != "running"]
    if extra_down:
        return _check("containers", label, WARN, f"{'、'.join(extra_down)} 未運行",
                      "網站本身正常，但管理用的容器沒有在跑。")
    return _check("containers", label, OK, "全部運行中")


def _disk_check() -> dict:
    sites_dir = settings.SITES_DIR
    usage = shutil.disk_usage(sites_dir if sites_dir.exists() else settings.EZWEB_ROOT)
    free_pct = usage.free / usage.total * 100
    value = f"剩餘 {human_size(usage.free)}（{free_pct:.0f}%）"
    status = OK if free_pct >= _DISK_WARN_PCT else WARN if free_pct >= _DISK_FAIL_PCT else FAIL
    detail = "" if status == OK else "主機磁碟空間偏低，備份和建立新站台可能會失敗。"
    return _check("disk", "主機磁碟", status, value, detail)


def _backup_check(db: Session, site: Site) -> dict:
    label = "最近備份"
    last = (
        db.query(Backup)
        .filter(Backup.site_id == site.id, Backup.status == "success")
        .order_by(Backup.created_at.desc())
        .first()
    )
    auto = {"daily": "每天自動備份", "weekly": "每週自動備份"}.get(site.backup_schedule, "未啟用自動備份")
    if last is None:
        return _check("backup", label, WARN, "尚未備份", auto)
    days = (datetime.utcnow() - last.created_at).total_seconds() / 86400
    value = "今天" if days < 1 else f"{int(days)} 天前"
    status = OK if days < _BACKUP_WARN_DAYS else WARN if days < _BACKUP_FAIL_DAYS else FAIL
    return _check("backup", label, status, value, auto)


def _php_check(version: str) -> dict:
    try:
        major, minor = (int(x) for x in version.split(".")[:2])
    except ValueError:
        return _check("php", "PHP 版本", INFO, version)
    if (major, minor) >= (8, 1):
        return _check("php", "PHP 版本", OK, version)
    if (major, minor) >= (7, 4):
        return _check("php", "PHP 版本", WARN, version, "此版本已停止官方安全性更新，建議升級到 8.1 以上。")
    return _check("php", "PHP 版本", FAIL, version, "版本過舊，WordPress 與多數外掛已不再支援。")


def _core_check(version: str, newer: str) -> dict:
    label = "WordPress 版本"
    if newer:
        return _check("core", label, WARN, version, f"有新版本可更新：{newer}")
    return _check("core", label, OK, version, "已是最新版本")


# ── Entry point ──────────────────────────────────────────────────────────────

async def check_health(db: Session, site: Site, user_id: int | None) -> dict:
    """Raises ValueError if the site isn't running (nothing to probe).

    The first five keys of the result predate the traffic-light report and
    are kept as-is for existing API callers."""
    if site.status != "running":
        raise ValueError(f"站台目前狀態為 {site.status}，無法進行健康檢查")
    site_dir = Path(site.site_path)

    def wp(args: list[str]):
        return _exec_wpcli(db, site, user_id, args, "health", log=False)

    # Only two of these boot WordPress (eval, verify-checksums) — each boot
    # costs seconds on a plugin-heavy site, so everything else is folded into
    # the one eval or asked of the container directly.
    (
        (probe_rc, probe_out, probe_err),
        (checksums_rc, _, checksums_err),
        (php_rc, php_out, _),
        (ps_rc, ps_out, ps_err),
        (du_rc, du_out, _),
    ) = await asyncio.gather(
        wp(["eval", _PROBE_PHP]),
        wp(["core", "verify-checksums"]),
        docker_runner.compose_exec(site_dir, "wordpress", "php", "-r", _PHP_PROBE),
        docker_runner.compose_ps(site_dir),
        docker_runner.compose_exec(site_dir, "wpcli", "du", "-sk", "/var/www/html/wp-content"),
    )

    info: dict = {}
    if probe_rc == 0 and _MARKER in probe_out:
        try:
            info = json.loads(probe_out.split(_MARKER, 1)[1])
        except ValueError:
            info = {}

    plugin_updates = int(info.get("plugin_updates") or 0)
    theme_updates = int(info.get("theme_updates") or 0)
    php_version, _, memory_limit = php_out.strip().partition("|") if php_rc == 0 else ("", "", "")
    stage = site.current_stage

    checks = [
        _containers_check(ps_rc, ps_out, ps_err),
        await _http_check(site, info.get("home")),
    ]

    if not info:
        checks.append(_check(
            "wordpress", "WordPress 載入", FAIL, "載入失敗",
            (probe_err or probe_out).strip()[-400:] or "wp-cli 無法啟動 WordPress，可能是外掛或佈景主題發生嚴重錯誤。",
        ))
    else:
        checks.append(_core_check(str(info.get("wp_version", "?")), str(info.get("core_update") or "")))
        checks.append(_check(
            "checksums", "核心檔案完整性",
            OK if checksums_rc == 0 else WARN,
            "與官方一致" if checksums_rc == 0 else "有檔案與官方不符",
            "" if checksums_rc == 0 else checksums_err.strip()[-300:],
        ))
        checks.append(_check(
            "plugin_updates", "外掛更新",
            OK if plugin_updates == 0 else WARN,
            "全部最新" if plugin_updates == 0 else f"{plugin_updates} 個可更新",
            f"啟用中的外掛：{info.get('active_plugins', 0)} 個",
        ))
        checks.append(_check(
            "theme_updates", "佈景主題更新",
            OK if theme_updates == 0 else WARN,
            "全部最新" if theme_updates == 0 else f"{theme_updates} 個可更新",
        ))
        if php_version:
            checks.append(_php_check(php_version))
            checks.append(_check("memory_limit", "PHP 記憶體上限", INFO, memory_limit or "?"))

        db_bytes = int(info.get("db_bytes") or 0)
        checks.append(_check(
            "db_size", "資料庫大小",
            OK if db_bytes < _DB_WARN else WARN if db_bytes < _DB_FAIL else FAIL,
            human_size(db_bytes),
            "" if db_bytes < _DB_WARN else "資料庫偏大，可考慮清理文章修訂版本與過期暫存資料。",
        ))

        autoload = int(info.get("autoload_bytes") or 0)
        checks.append(_check(
            "autoload", "自動載入選項",
            OK if autoload < _AUTOLOAD_WARN else WARN if autoload < _AUTOLOAD_FAIL else FAIL,
            human_size(autoload),
            "每次載入頁面都會讀取這些資料。" + ("" if autoload < _AUTOLOAD_WARN else "超過 800 KB 會拖慢每一個頁面。"),
        ))

        if info.get("wp_debug"):
            shown = bool(info.get("wp_debug_display"))
            status = FAIL if (stage == "prod" and shown) else WARN
            checks.append(_check(
                "debug", "除錯模式", status, "開啟" + ("（錯誤顯示在頁面上）" if shown else ""),
                "正式環境不應開啟 WP_DEBUG。" if stage == "prod" else "上線前記得關閉 WP_DEBUG。",
            ))
        else:
            checks.append(_check("debug", "除錯模式", OK, "關閉"))

        public = bool(info.get("blog_public"))
        if stage == "prod":
            checks.append(_check(
                "search", "搜尋引擎能見度", OK if public else WARN,
                "允許索引" if public else "已阻擋搜尋引擎",
                "" if public else "PROD 階段的站台通常應該允許搜尋引擎索引（設定 → 閱讀）。",
            ))
        else:
            checks.append(_check(
                "search", "搜尋引擎能見度", INFO,
                "允許索引" if public else "已阻擋搜尋引擎",
                "非正式階段的站台，建議阻擋搜尋引擎。" if public else "",
            ))

    if du_rc == 0 and du_out.split():
        try:
            checks.append(_check("content_size", "wp-content 大小", INFO, human_size(int(du_out.split()[0]) * 1024)))
        except ValueError:
            pass

    checks.append(_disk_check())
    checks.append(_backup_check(db, site))

    summary = {s: sum(1 for c in checks if c["status"] == s) for s in (OK, WARN, FAIL)}
    overall = max((c["status"] for c in checks), key=lambda s: _SEVERITY[s])
    if overall == INFO:
        overall = OK

    return {
        "site_status": site.status,
        "wp_installed": bool(info),
        "checksums_ok": checksums_rc == 0,
        "plugin_updates_available": plugin_updates,
        "theme_updates_available": theme_updates,
        "overall": overall,
        "summary": summary,
        "checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "checks": checks,
    }
