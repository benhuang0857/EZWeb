# EZWeb

Self-hosted platform for provisioning and managing multiple WordPress sites (Docker Compose) through a FastAPI + htmx web UI.

## Layout

- `web-ui/` — FastAPI app (site management, nginx config generation)
- `wp-ezweb/` — WordPress/MariaDB/phpMyAdmin Docker Compose template
- `plugin/` — WordPress plugin bundles

## Install (Linux)

```bash
./install.sh --service
```

The installer checks for and installs anything missing — curl, Python 3.10+
(with venv), Docker, Docker Compose, docker group membership — then creates
`web-ui/.venv`, installs the Python packages, generates `web-ui/.env` with
random `SECRET_KEY` / `API_KEY`, and (with `--service`) runs EZWeb as the
`ezweb` systemd service on port 3000. It is safe to re-run.

| Option | |
|---|---|
| `--check` | Only report what is missing; change nothing |
| `--yes` | Don't prompt (for automation) |
| `--service` | Install and start the `ezweb` systemd service |
| `--with-nginx` | Also install nginx + certbot (for domains / HTTPS) |
| `--port N` | web-ui port (default 3000) |

Supported: Ubuntu / Debian, Fedora / RHEL / Rocky / Alma.

Without `--service`, start it manually with `./web-ui/start.sh`.

Site templates (`theme/*.wpress`) are not in git — copy at least one in
before creating a site.

Default login: `admin` / `admin` — change it immediately.

Nginx/certbot setup for a site's domain is applied manually.
