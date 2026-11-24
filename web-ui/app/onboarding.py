"""
First-run guide shown on the dashboard until the basic setup is done:
change the default password → upload a template → create a site → see it run.

Every step's "done" state is derived from real state (password hash,
files in theme/, rows in sites), never stored, so the guide can't drift
out of sync with reality.
"""
from dataclasses import dataclass

from app.auth.service import verify_password
from app.models import Site, User

DEFAULT_PASSWORD = "admin"

# bcrypt is deliberately slow (~0.2 s); cache the answer per password hash.
# A password change produces a new hash, so stale entries are harmless.
_default_pw_cache: dict[str, bool] = {}


def uses_default_password(user: User) -> bool:
    h = user.password_hash
    if h not in _default_pw_cache:
        if len(_default_pw_cache) > 256:
            _default_pw_cache.clear()
        _default_pw_cache[h] = verify_password(DEFAULT_PASSWORD, h)
    return _default_pw_cache[h]


@dataclass
class Step:
    key: str
    title: str
    done: bool


@dataclass
class Guide:
    steps: list[Step]
    current: str | None          # key of the first unfinished step
    building: bool               # a site exists but none is running yet
    has_error: bool              # ...and at least one of them failed
    running_site: Site | None    # first running site, for the "open it" link

    @property
    def complete(self) -> bool:
        return self.current is None

    @property
    def done_count(self) -> int:
        return sum(s.done for s in self.steps)


def build_guide(user: User, template_names: list[str], sites: list[Site]) -> Guide:
    running = [s for s in sites if s.status == "running"]
    steps = [
        Step("password", "修改預設密碼", not uses_default_password(user)),
        Step("template", "上傳母版", bool(template_names)),
        Step("site", "建立第一個站台", bool(sites)),
        Step("running", "等待建置完成並開啟網站", bool(running)),
    ]
    current = next((s.key for s in steps if not s.done), None)
    return Guide(
        steps=steps,
        current=current,
        building=bool(sites) and not running,
        has_error=bool(sites) and not running and any(s.status == "error" for s in sites),
        running_site=running[0] if running else None,
    )
