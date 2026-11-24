"""
Async wrappers around docker compose commands.
All operations run in a thread-pool executor using subprocess.run so that they
work on Windows (asyncio.create_subprocess_exec requires ProactorEventLoop which
uvicorn does not use on Windows).
"""
import asyncio
import shlex
import subprocess
from pathlib import Path

from app.config import settings

TIMEOUT = 600  # seconds – building wpcli image + wp restore can take a while


async def _run(site_dir: Path, *args: str, timeout: int = TIMEOUT) -> tuple[int, str, str]:
    cmd = shlex.split(settings.DOCKER_COMPOSE_CMD) + list(args)
    loop = asyncio.get_running_loop()

    def _blocking() -> tuple[int, str, str]:
        try:
            result = subprocess.run(
                cmd,
                cwd=str(site_dir),
                capture_output=True,
                timeout=timeout,
            )
            return (
                result.returncode,
                result.stdout.decode(errors="replace"),
                result.stderr.decode(errors="replace"),
            )
        except subprocess.TimeoutExpired:
            return -1, "", f"Timed out after {timeout}s"
        except FileNotFoundError as e:
            return -1, "", str(e)

    return await loop.run_in_executor(None, _blocking)


async def compose_up(site_dir: Path) -> tuple[int, str, str]:
    return await _run(site_dir, "up", "-d", "--build")


async def compose_auto_config(site_dir: Path) -> tuple[int, str, str]:
    return await _run(
        site_dir,
        "-f", "docker-compose.yml",
        "-f", "wp-auto-config.yml",
        "run", "--rm", "wp-auto-config",
    )


async def compose_stop(site_dir: Path) -> tuple[int, str, str]:
    """Pause: stop containers but keep volumes/data."""
    return await _run(site_dir, "stop")


async def compose_start(site_dir: Path) -> tuple[int, str, str]:
    """Resume stopped containers."""
    return await _run(site_dir, "start")


async def compose_restart(site_dir: Path, *services: str) -> tuple[int, str, str]:
    """Restart already-running service(s) — needed after rewriting a bind-mounted
    config file (e.g. php.conf.ini): PHP only reads php.ini at process start, so
    the new values don't take effect until the container's PHP process restarts."""
    return await _run(site_dir, "restart", *services)


async def compose_down(site_dir: Path) -> tuple[int, str, str]:
    """Destroy containers, networks AND the site's named volumes.

    Only used when deleting a site: the site directory and DB row go away
    too, so a kept WORDPRESS_DATA_DIR volume (the whole WordPress install,
    ~1 GB with a typical template) would be unreachable dead weight.
    --remove-orphans also catches containers from wp-auto-config.yml, which
    is a separate compose file and so looks like an orphan to this one.
    """
    return await _run(site_dir, "down", "-v", "--remove-orphans")


async def run_docker(*args: str, timeout: int = 120) -> tuple[int, str, str]:
    """Run a plain `docker ...` command (not compose). The docker binary is
    taken from DOCKER_COMPOSE_CMD so a custom path there applies here too."""
    docker = shlex.split(settings.DOCKER_COMPOSE_CMD)[0]
    if docker == "docker-compose":
        docker = "docker"
    cmd = [docker, *args]
    loop = asyncio.get_running_loop()

    def _blocking() -> tuple[int, str, str]:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=timeout)
            return r.returncode, r.stdout.decode(errors="replace"), r.stderr.decode(errors="replace")
        except subprocess.TimeoutExpired:
            return -1, "", f"Timed out after {timeout}s"
        except FileNotFoundError as e:
            return -1, "", str(e)

    return await loop.run_in_executor(None, _blocking)


async def compose_exec(site_dir: Path, service: str, *args: str) -> tuple[int, str, str]:
    """Run an arbitrary command inside a running compose service's container."""
    return await _run(site_dir, "exec", "-T", service, *args)


async def wp_cli(site_dir: Path, *wp_args: str) -> tuple[int, str, str]:
    """Run `wp <wp_args>` inside the site's long-lived wpcli container."""
    return await compose_exec(site_dir, "wpcli", "wp", *wp_args)


async def compose_cp_to(site_dir: Path, host_src: Path, service: str, container_dest: str) -> tuple[int, str, str]:
    """`docker compose cp <host_src> <service>:<container_dest>` — copies a
    host file into a running service's container/volume. container_dest's
    parent directory must already exist (compose cp won't create it — use
    compose_exec(..., "mkdir", "-p", ...) first if needed)."""
    return await _run(site_dir, "cp", str(host_src), f"{service}:{container_dest}")


async def compose_exec_as(
    site_dir: Path, service: str, user: str, *args: str, timeout: int = TIMEOUT
) -> tuple[int, str, str]:
    """compose_exec as a specific container user (e.g. "0:0" for root)."""
    return await _run(site_dir, "exec", "-T", "--user", user, service, *args, timeout=timeout)


async def wp_cli_long(site_dir: Path, *wp_args: str, timeout: int) -> tuple[int, str, str]:
    """wp_cli with a caller-chosen timeout, for operations that can outlast
    the default (a full-site backup of a large site)."""
    return await _run(site_dir, "exec", "-T", "wpcli", "wp", *wp_args, timeout=timeout)


async def compose_cp_from(
    site_dir: Path, service: str, container_src: str, host_dest: Path, timeout: int = TIMEOUT
) -> tuple[int, str, str]:
    """`docker compose cp <service>:<container_src> <host_dest>` — copies a
    file out of a running service's container/volume onto the host."""
    return await _run(site_dir, "cp", f"{service}:{container_src}", str(host_dest), timeout=timeout)


async def compose_ps(site_dir: Path) -> tuple[int, str, str]:
    """State of every container of the project (including stopped ones), as
    JSON. Compose prints one object per line (v2.21+) or a single array
    (older) — callers must accept both."""
    return await _run(site_dir, "ps", "--all", "--format", "json", timeout=60)
