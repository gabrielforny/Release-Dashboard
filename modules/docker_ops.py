"""
Docker operations — subprocess wrappers so make / docker CLI work normally.
"""

import subprocess
from typing import Optional, Tuple


class DockerError(Exception):
    pass


def _run(
    cmd: list[str],
    cwd: Optional[str] = None,
    timeout: int = 600,
) -> Tuple[str, str]:
    result = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise DockerError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip(), result.stderr.strip()


def make_docker_file(cwd: Optional[str] = None, timeout: int = 1200) -> str:
    # O Makefile usa "sudo docker build", que trava sem TTY aguardando senha.
    # Chamamos docker build diretamente — o sg docker no dashboard.sh já
    # garante que o processo tem permissão do grupo docker sem precisar de sudo.
    out, _ = _run(
        ["docker", "build", "-f", "Dockerfile", "-t", "zcrm_front", "."],
        cwd=cwd,
        timeout=timeout,
    )
    return out


def tag_image(source: str, target: str) -> str:
    out, _ = _run(["docker", "tag", source, target])
    return out


def push_image(tag: str, timeout: int = 300) -> str:
    out, _ = _run(["docker", "push", tag], timeout=timeout)
    return out


def image_exists(tag: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", tag],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def is_logged_in(registry: str = "") -> bool:
    """Check Docker login by trying to inspect config."""
    import json
    import os

    config_path = os.path.expanduser("~/.docker/config.json")
    if not os.path.exists(config_path):
        return False
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        auths = cfg.get("auths", {})
        if not registry:
            return bool(auths)
        return any(registry in key for key in auths)
    except Exception:
        return False
