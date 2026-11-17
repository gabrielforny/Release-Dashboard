"""
Docker operations — subprocess wrappers so make / docker CLI work normally.
"""

import subprocess
import sys
import time
from typing import Optional, Tuple


class DockerError(Exception):
    pass


def _run(
    cmd: list[str],
    cwd: Optional[str] = None,
    timeout: int = 600,
) -> Tuple[str, str]:
    """Silent run — captures output and returns it. Use for fast commands."""
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


def _run_streaming(
    cmd: list[str],
    cwd: Optional[str] = None,
    timeout: int = 1200,
) -> None:
    """
    Runs cmd and streams stdout+stderr to Python's stdout in real-time so the
    dashboard SSE loop picks up each line as it arrives.
    Raises DockerError on non-zero exit or timeout.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    start = time.time()
    output_lines: list[str] = []
    try:
        for line in proc.stdout:
            if time.time() - start > timeout:
                proc.kill()
                proc.wait()
                raise DockerError(f"Timeout após {timeout}s")
            stripped = line.rstrip("\n")
            output_lines.append(stripped)
            print(stripped, flush=True)
    finally:
        proc.stdout.close()
        proc.wait()

    if proc.returncode != 0:
        tail = "\n".join(output_lines[-20:])
        raise DockerError(f"Saiu com código {proc.returncode}\n{tail}")


def make_docker_file(cwd: Optional[str] = None, timeout: int = 1200) -> str:
    # O Makefile usa "sudo docker build", que trava sem TTY aguardando senha.
    # Chamamos docker build diretamente — o sg docker no dashboard.sh já
    # garante que o processo tem permissão do grupo docker sem precisar de sudo.
    # Usa streaming para que o dashboard mostre cada camada em tempo real.
    _run_streaming(
        ["docker", "build", "-f", "Dockerfile", "-t", "zcrm_front", "."],
        cwd=cwd,
        timeout=timeout,
    )
    return ""


def tag_image(source: str, target: str) -> str:
    out, _ = _run(["docker", "tag", source, target])
    return out


def push_image(tag: str, timeout: int = 600) -> str:
    # Streaming so the dashboard shows push-layer progress in real time.
    _run_streaming(["docker", "push", tag], timeout=timeout)
    return ""


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
