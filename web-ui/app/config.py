from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).parent.parent / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    SECRET_KEY: str = "change-me"
    SESSION_MAX_AGE: int = 3600
    API_KEY: str = ""

    PORT_RANGE_START: int = 8100
    PORT_RANGE_END: int = 8999

    DOCKER_COMPOSE_CMD: str = "docker compose"
    NGINX_CONFIG_DIR: str = "data/nginx-configs"

    # Rate limit for destructive per-site endpoints (cleanup execute, plugin
    # zip install) — see app/sites/service.py::check_destructive_rate_limit.
    # Untuned defaults; adjust once real Dashboard usage patterns are known.
    DESTRUCTIVE_OP_MAX: int = 5
    DESTRUCTIVE_OP_WINDOW_SEC: int = 300

    # Max upload size accepted by POST /{site_id}/plugins/install-zip.
    MAX_PLUGIN_ZIP_MB: int = 50

    # Max size of a site template (.wpress) uploaded through the web UI.
    MAX_TEMPLATE_UPLOAD_MB: int = 5120

    # Host name / IP that browsers use to reach this server. Used for site
    # links in the UI and as the WordPress URL of sites created without a
    # domain. Empty = links use whatever host the browser used, and new sites
    # fall back to "localhost" (only reachable from this machine).
    PUBLIC_HOST: str = ""

    # Derived paths (resolved relative to EZWeb root)
    @property
    def EZWEB_ROOT(self) -> Path:
        return Path(__file__).parent.parent.parent.resolve()

    @property
    def TEMPLATE_DIR(self) -> Path:
        return self.EZWEB_ROOT / "wp-ezweb"

    @property
    def WPRESS_DIR(self) -> Path:
        return self.EZWEB_ROOT / "theme"

    @property
    def PLUGIN_DIR(self) -> Path:
        return self.EZWEB_ROOT / "plugin"

    @property
    def SITES_DIR(self) -> Path:
        return self.EZWEB_ROOT / "sites"

    @property
    def DB_PATH(self) -> Path:
        p = Path(__file__).parent.parent / "data"
        p.mkdir(parents=True, exist_ok=True)
        return p / "webui.db"

    @property
    def NGINX_CONFIGS_DIR(self) -> Path:
        p = Path(__file__).parent.parent / self.NGINX_CONFIG_DIR
        p.mkdir(parents=True, exist_ok=True)
        return p


settings = Settings()
