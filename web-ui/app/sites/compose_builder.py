"""
Builds the file-system structure for each site under SITES_DIR/{site_id}/.
Copies/generates all files needed to run docker compose.
"""
import secrets
import shutil
from pathlib import Path

from app.config import settings


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def build_site_files(
    site_id: str,
    wp_port: int,
    pma_port: int,
    template_id: str,
    wp_title: str,
    wp_admin_user: str,
    wp_admin_pass: str,
    wp_admin_email: str,
    db_password: str,
    domain: str | None = None,
) -> Path:
    """
    Creates SITES_DIR/{site_id}/ with all required files.
    Returns the site directory path.
    """
    tmpl = settings.TEMPLATE_DIR
    site_dir = settings.SITES_DIR / site_id
    site_dir.mkdir(parents=True, exist_ok=True)

    # ── config/ ──────────────────────────────────────────────────────────
    cfg_dst = site_dir / "config"
    cfg_dst.mkdir(exist_ok=True)
    shutil.copy2(tmpl / "config" / "php.conf.ini", cfg_dst / "php.conf.ini")

    wp_cfg_dst = cfg_dst / "wordpress"
    wp_cfg_dst.mkdir(exist_ok=True)

    # Copy All-In-One plugin zip
    plugin_zip = tmpl / "config" / "wordpress" / "All-In-One-WP-Migration-With-Import-master.zip"
    if plugin_zip.exists():
        shutil.copy2(plugin_zip, wp_cfg_dst / plugin_zip.name)

    # Copy chosen .wpress template — must sit at config/wordpress/<template_id>.wpress
    # to match the host-side path in wp-auto-config.yml.org volume mount.
    wpress_src = settings.WPRESS_DIR / f"{template_id}.wpress"
    if not wpress_src.exists():
        raise FileNotFoundError(f"Template not found: {wpress_src}")
    shutil.copy2(wpress_src, wp_cfg_dst / f"{template_id}.wpress")

    # ── wpcli/ ───────────────────────────────────────────────────────────
    wpcli_src = tmpl / "wpcli"
    wpcli_dst = site_dir / "wpcli"
    if wpcli_dst.exists():
        shutil.rmtree(wpcli_dst)
    shutil.copytree(wpcli_src, wpcli_dst)

    # Replace {{template_id}} in Makefile (generated from Makefile.org)
    makefile_org = wpcli_dst / "Makefile.org"
    if makefile_org.exists():
        content = _read(makefile_org).replace("{{template_id}}", template_id)
        _write(wpcli_dst / "Makefile", content)

    # ── empty dirs ───────────────────────────────────────────────────────
    (site_dir / "mysql").mkdir(exist_ok=True)

    # ── docker-compose.yml ───────────────────────────────────────────────
    shutil.copy2(tmpl / "docker-compose.yml", site_dir / "docker-compose.yml")

    # ── wp-auto-config.yml ───────────────────────────────────────────────
    auto_cfg_org = tmpl / "wp-auto-config.yml.org"
    auto_cfg_content = _read(auto_cfg_org).replace("{{template_id}}", template_id)
    _write(site_dir / "wp-auto-config.yml", auto_cfg_content)

    # ── .env ─────────────────────────────────────────────────────────────
    # Without a domain WordPress is reached at host:port. WordPress redirects
    # to its configured URL, so "localhost" here would bounce every remote
    # browser to its *own* machine — use the server's public host if known.
    host = settings.PUBLIC_HOST or "localhost"
    wp_url = f"http://{domain}" if domain else f"http://{host}:{wp_port}"
    wp_url_no_http = domain if domain else f"{host}:{wp_port}"
    db_root_pass = secrets.token_urlsafe(16)

    env_content = f"""\
COMPOSE_PROJECT_NAME={site_id}
WORDPRESS_VERSION=latest
MARIADB_VERSION=latest
PHPMYADMIN_VERSION=latest

DATABASE_USER=wpuser
DATABASE_PASSWORD={db_password}
MYSQL_ROOT_PASSWORD={db_root_pass}
WORDPRESS_DB_HOST=mysql

WORDPRESS_WEBSITE_PORT={wp_port}
PHPMYADMIN_PORT={pma_port}

WORDPRESS_LOCALE=zh_TW
WORDPRESS_WEBSITE_TITLE={wp_title}
WORDPRESS_WEBSITE_URL={wp_url}
WORDPRESS_WEBSITE_URL_WITHOUT_HTTP={wp_url_no_http}
WORDPRESS_WEBSITE_POST_URL_STRUCTURE=/blog/%postname%/

WORDPRESS_ADMIN_USER={wp_admin_user}
WORDPRESS_ADMIN_PASSWORD={wp_admin_pass}
WORDPRESS_ADMIN_EMAIL={wp_admin_email}

WAIT_BEFORE_HOSTS=30
WAIT_AFTER_HOSTS=15
WAIT_HOSTS_TIMEOUT=300
WAIT_SLEEP_INTERVAL=30
WAIT_HOST_CONNECT_TIMEOUT=30
"""
    _write(site_dir / ".env", env_content)

    return site_dir
