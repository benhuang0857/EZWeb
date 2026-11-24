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

## Managing a site

Each site has a **管理** page (`/sites/<id>`) with three sections. Everything
on it is also available through the JSON API, authenticated with that site's
API key (`Authorization: Bearer <key>`; mint one with
`POST /api/sites/<id>/rotate-key` using the fleet `API_KEY`).

**Plugins** — installed plugins with status, version and available updates;
activate / deactivate / update (one, or all that have an update). Premium
plugins whose package can't be downloaded without a licence fail to update
and are reported as such.

| | |
|---|---|
| `GET /api/sites/<id>/plugins` | List plugins |
| `POST /api/sites/<id>/plugins/<slug>/activate` | Activate |
| `POST /api/sites/<id>/plugins/<slug>/deactivate` | Deactivate |
| `POST /api/sites/<id>/plugins/<slug>/update` | Update one plugin |
| `POST /api/sites/<id>/plugins/update-all` | Update all (synchronous, can take minutes) |

**Health check** — `GET /api/sites/<id>/health` returns an `overall` light
(`ok` / `warn` / `fail`) and a `checks` list, each with a `value` and its own
`status`: containers, HTTP response and time, WordPress / PHP version, core
file integrity, pending plugin and theme updates, database size, autoloaded
options, debug mode, search-engine visibility, wp-content size, host disk and
the age of the last backup.

**Backups** — full-site All-in-One WP Migration exports (`.wpress`), run by
hand or on a schedule (daily / weekly at a chosen hour, keeping the newest N
automatic backups). Files are stored on the host in `backups/<id>/` (override
with `BACKUP_DIR` in `web-ui/.env`), can be downloaded, and can be saved as a
site template to clone the site.

| | |
|---|---|
| `GET /api/sites/<id>/backups` | List backups |
| `POST /api/sites/<id>/backups` | Start one (returns `202`, status `running`) |
| `GET /api/sites/<id>/backups/<n>` | Status of one backup |
| `GET /api/sites/<id>/backups/<n>/download` | Download the `.wpress` |
| `DELETE /api/sites/<id>/backups/<n>` | Delete |
| `GET` / `PUT /api/sites/<id>/backups/schedule` | `{"schedule": "off\|daily\|weekly", "hour": 3, "keep": 5}` |

Interactive API docs: `/docs`.
