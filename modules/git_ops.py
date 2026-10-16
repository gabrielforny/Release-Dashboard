"""
Git operations — thin wrappers around subprocess git calls.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional, Tuple


class GitError(Exception):
    pass


# SSH options injected into push/fetch commands to avoid hanging on bad connections
_SSH_CMD = "ssh -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"


def _run(
    cmd: list[str],
    cwd: Optional[str] = None,
    timeout: int = 120,
    ssh: bool = False,
) -> Tuple[str, str]:
    """Run a git command and return (stdout, stderr). Raises GitError on non-zero exit."""
    env = None
    if ssh:
        env = {**os.environ, "GIT_SSH_COMMAND": _SSH_CMD}
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"Comando git expirou após {timeout}s: {' '.join(cmd)}")
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip(), result.stderr.strip()


def status(cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "status", "--porcelain"], cwd=cwd)
    return out


def fetch_all(cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "fetch", "--all"], cwd=cwd, timeout=120, ssh=True)
    return out


def current_branch(cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return out


def checkout(branch: str, cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "checkout", branch], cwd=cwd)
    return out


def checkout_new(branch: str, cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "checkout", "-b", branch], cwd=cwd)
    return out


def pull(branch: str, cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "pull", "origin", branch], cwd=cwd, timeout=120, ssh=True)
    return out


def _resolve_merge_ref(branch: str, cwd: Optional[str] = None) -> str:
    """
    Resolve um nome de branch para uma ref que o git consegue mergear.
    Branches formalizadas via Slack normalmente só existem como ref remota
    (origin/<branch>) após 'git fetch --all' — nunca foram checked out
    localmente, então 'git merge <branch>' falha com "not something we
    can merge". Prefere a branch local se ela existir; senão usa origin/.
    """
    local = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=cwd,
        capture_output=True,
    )
    if local.returncode == 0:
        return branch

    remote = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
        cwd=cwd,
        capture_output=True,
    )
    if remote.returncode == 0:
        return f"origin/{branch}"

    return branch


def merge(branch: str, cwd: Optional[str] = None) -> Tuple[str, bool]:
    """Returns (output, had_conflict). Does NOT raise on conflict."""
    ref = _resolve_merge_ref(branch, cwd=cwd)
    env = {**os.environ, "GIT_SSH_COMMAND": _SSH_CMD}
    try:
        result = subprocess.run(
            ["git", "merge", ref],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            env=env,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["git", "merge", "--abort"], cwd=cwd, capture_output=True)
        return f"Merge de '{branch}' expirou após 120s", True
    output = result.stdout.strip()
    if result.returncode != 0:
        conflict_output = result.stdout.strip() + "\n" + result.stderr.strip()
        # Abort the failed merge so the tree stays clean
        subprocess.run(["git", "merge", "--abort"], cwd=cwd, capture_output=True)
        return conflict_output, True
    return output, False


def add(paths: list[str], cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "add"] + paths, cwd=cwd)
    return out


def commit(message: str, cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "commit", "-m", message], cwd=cwd)
    return out


def push(branch: str, cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "push", "origin", branch], cwd=cwd, timeout=120, ssh=True)
    return out


def push_tags(cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "push", "--tags"], cwd=cwd, timeout=120, ssh=True)
    return out


def tag(name: str, message: str, cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "tag", "-a", name, "-m", message], cwd=cwd)
    return out


def latest_tag(cwd: Optional[str] = None) -> str:
    """Retorna a tag mais recentemente criada (por data de criação, não ordem alfabética)."""
    out, _ = _run(
        ["git", "for-each-ref", "refs/tags", "--sort=-creatordate", "--format=%(refname:short)"],
        cwd=cwd,
    )
    lines = out.splitlines()
    return lines[0] if lines else ""


def list_tags(pattern: str = "", cwd: Optional[str] = None) -> list[str]:
    cmd = ["git", "tag", "-l"]
    if pattern:
        cmd.append(pattern)
    out, _ = _run(cmd, cwd=cwd)
    return [t for t in out.splitlines() if t]


def merge_keep_ours(
    branch: str, ours_paths: list[str], cwd: Optional[str] = None
) -> Tuple[str, bool]:
    """
    Faz merge de 'branch' na branch atual, garantindo que os arquivos listados em
    'ours_paths' (ex: package.json) permaneçam com o conteúdo da branch atual —
    "opção current change", conforme o processo de hotfix.

    Importante: o conteúdo de 'ours_paths' é restaurado para o estado anterior ao
    merge e recommitado SEMPRE, mesmo quando o git não sinaliza conflito textual
    (isso acontece quando o arquivo não mudou na branch atual desde o ancestral
    comum — nesse caso o git adotaria silenciosamente a versão da outra branch,
    o que violaria a regra de negócio de manter a versão da release atual).

    Se houver conflito em qualquer arquivo FORA de 'ours_paths', aborta e retorna
    had_conflict=True (igual ao comportamento de merge()), para resolução manual.
    """
    ref = _resolve_merge_ref(branch, cwd=cwd)

    # Snapshot do conteúdo atual dos arquivos protegidos, antes do merge.
    preserved: dict[str, bytes] = {}
    base = Path(cwd) if cwd else Path(".")
    for rel_path in ours_paths:
        p = base / rel_path
        if p.exists():
            preserved[rel_path] = p.read_bytes()

    result = subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", ref],
        cwd=cwd,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        status_result = subprocess.run(
            ["git", "diff", "--name-only", "--diff-filter=U"],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        conflicted = [f.strip() for f in status_result.stdout.splitlines() if f.strip()]
        unexpected = [f for f in conflicted if f not in ours_paths]

        if not conflicted or unexpected:
            conflict_output = result.stdout.strip() + "\n" + result.stderr.strip()
            subprocess.run(["git", "merge", "--abort"], cwd=cwd, capture_output=True)
            return conflict_output, True

    # Força os arquivos protegidos de volta ao conteúdo pré-merge, independente
    # de terem gerado conflito textual ou sido mesclados silenciosamente pelo git.
    for rel_path, content in preserved.items():
        (base / rel_path).write_bytes(content)
        subprocess.run(["git", "add", rel_path], cwd=cwd, capture_output=True)

    commit_result = subprocess.run(
        ["git", "commit", "--no-edit"], cwd=cwd, capture_output=True, text=True
    )
    if commit_result.returncode != 0:
        subprocess.run(["git", "merge", "--abort"], cwd=cwd, capture_output=True)
        return commit_result.stderr.strip() or commit_result.stdout.strip(), True

    return (
        f"Merge concluído; {', '.join(preserved) or 'arquivos protegidos'} mantidos "
        f"com a versão da release atual",
        False,
    )


def branch_exists_remote(branch: str, cwd: Optional[str] = None) -> bool:
    env = {**os.environ, "GIT_SSH_COMMAND": _SSH_CMD}
    try:
        result = subprocess.run(
            ["git", "ls-remote", "--heads", "origin", branch],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        return bool(result.stdout.strip())
    except subprocess.TimeoutExpired:
        return False


def rev_parse_head(cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "rev-parse", "HEAD"], cwd=cwd)
    return out


def remote_head(branch: str, cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "rev-parse", f"origin/{branch}"], cwd=cwd)
    return out


def log_oneline(branch: str, base: str = "HEAD~10", cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "log", "--oneline", f"{base}..{branch}"], cwd=cwd)
    return out


def user_name(cwd: Optional[str] = None) -> str:
    result = subprocess.run(
        ["git", "config", "user.name"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def push_new_branch(branch: str, cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "push", "-u", "origin", branch], cwd=cwd, timeout=120, ssh=True)
    return out


def reset_hard(ref: str, cwd: Optional[str] = None) -> str:
    out, _ = _run(["git", "reset", "--hard", ref], cwd=cwd)
    return out


def set_version_in_package_json(
    path: str, version: str, crm_version: str
) -> None:
    import json

    with open(path, "r", encoding="utf-8") as f:
        pkg = json.load(f)

    pkg["version"] = version
    pkg["crmVersion"] = crm_version

    with open(path, "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=2, ensure_ascii=False)
        f.write("\n")
