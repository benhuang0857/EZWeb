"""
Site templates ("母版"): All-in-One WP Migration .wpress archives stored in
settings.WPRESS_DIR. A template's id is its filename without the extension.

The id is substituted verbatim into the per-site wp-auto-config.yml and
wpcli Makefile (see compose_builder), so it is restricted to a conservative
character set — never relax NAME_RE without escaping those call sites.
"""
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.config import settings

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
PART_PREFIX = ".upload-"

# .wpress layout: a sequence of [header][file bytes]. Each header is
# name(255) + size(14) + mtime(12) + path(4096) bytes, NUL-padded.
WPRESS_HEADER_SIZE = 255 + 14 + 12 + 4096

# Keep this much disk free after an upload so creating sites (each copies the
# template) and Docker don't immediately run out of space.
DISK_RESERVE_BYTES = 1024 ** 3


class TemplateError(ValueError):
    pass


@dataclass
class TemplateInfo:
    name: str
    size: int
    modified: datetime
    used_by: int = 0

    @property
    def size_human(self) -> str:
        return human_size(self.size)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def template_path(name: str) -> Path:
    return settings.WPRESS_DIR / f"{name}.wpress"


def validate_name(name: str) -> str:
    name = (name or "").strip()
    if name.lower().endswith(".wpress"):
        name = name[: -len(".wpress")]
    if not NAME_RE.match(name):
        raise TemplateError(
            "母版名稱只能使用英文字母、數字、點、底線和連字號（-），"
            "且必須以英文字母或數字開頭，最多 100 個字元。"
        )
    return name


def looks_like_wpress(header: bytes) -> bool:
    """Cheap sanity check on the first entry header of a .wpress archive."""
    if len(header) < WPRESS_HEADER_SIZE:
        return False
    name = header[:255].rstrip(b"\0")
    size = header[255:269].rstrip(b"\0")
    mtime = header[269:281].rstrip(b"\0")
    if not name or b"\0" in name or not all(32 <= c < 127 for c in name):
        return False
    return size.isdigit() and (not mtime or mtime.isdigit())


def list_templates(usage: dict[str, int] | None = None) -> list[TemplateInfo]:
    usage = usage or {}
    out = []
    for p in settings.WPRESS_DIR.glob("*.wpress"):
        st = p.stat()
        out.append(TemplateInfo(
            name=p.stem,
            size=st.st_size,
            modified=datetime.fromtimestamp(st.st_mtime),
            used_by=usage.get(p.stem, 0),
        ))
    return sorted(out, key=lambda t: t.name.lower())


def free_bytes() -> int:
    return shutil.disk_usage(settings.WPRESS_DIR).free


def check_space(incoming: int) -> None:
    free = free_bytes()
    if incoming + DISK_RESERVE_BYTES > free:
        raise TemplateError(
            f"磁碟空間不足：檔案 {human_size(incoming)}，目前可用 {human_size(free)}"
            f"（需保留至少 {human_size(DISK_RESERVE_BYTES)}）。"
        )


def delete_template(name: str) -> None:
    name = validate_name(name)
    p = template_path(name)
    if not p.exists():
        raise TemplateError(f"找不到母版：{name}")
    p.unlink()


def cleanup_partial_uploads() -> int:
    """Remove .part files left behind by uploads interrupted by a restart."""
    settings.WPRESS_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for p in settings.WPRESS_DIR.glob(f"{PART_PREFIX}*.part"):
        try:
            p.unlink()
            n += 1
        except OSError:
            pass
    return n
