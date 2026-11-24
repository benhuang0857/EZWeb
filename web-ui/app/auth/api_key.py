import hashlib
import secrets

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Site, User

# auto_error=False so a missing token gives our own 401 message below instead
# of FastAPI's default 403 "Not authenticated". Declaring this as a dependency
# is also what makes Swagger UI show the global padlock/Authorize button.
_bearer_scheme = HTTPBearer(auto_error=False)


def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """Fleet-admin auth for app/api/* routes that act across the whole fleet
    (list/create/delete sites, rotate a site's key) — separate from the cookie
    session used by the browser/htmx UI. Acts as the first admin user for
    ownership/audit-log purposes. Deliberately NOT accepted on site-scoped
    routes (see require_site_api_key) — a caller that only needs to operate on
    one site should never need to hold a credential that can reach every site."""
    if not settings.API_KEY:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API key auth is not configured")
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    if not secrets.compare_digest(credentials.credentials, settings.API_KEY):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    user = db.query(User).filter(User.is_admin.is_(True)).order_by(User.id).first()
    if user is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "No admin user found")
    return user


def require_site_api_key(
    site_id: str,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: Session = Depends(get_db),
) -> Site:
    """Per-site auth for app/api/sites/{site_id}/* operational routes (wpcli,
    cleanup, health, domains, pause/resume, plugin zip install, and reading
    the site's own status). The caller is authenticated as that site
    specifically — a leaked/misused key only exposes the one site it belongs
    to, never the rest of the fleet. No fallback to the global settings.API_KEY:
    that key is reserved for fleet-admin actions (see require_api_key) and
    must not double as a site credential."""
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    site = db.get(Site, site_id)
    if site is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Site not found")
    if not site.api_key_hash:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Site has no API key configured — call POST /{site_id}/rotate-key first",
        )
    computed = hashlib.sha256(credentials.credentials.encode()).hexdigest()
    if not secrets.compare_digest(computed, site.api_key_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid API key")
    return site
