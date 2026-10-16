#!/usr/bin/env python3
"""
ZCRM Release Automation — entry point.

Usage:
    # Roda de dentro do repositório frontend (auto-detecta tudo):
    python /path/to/release.py

    # Ou apontando para o repo:
    python release.py --repo /path/to/frontend

    # CI mode (sem perguntas):
    python release.py --repo /path/to/frontend --yes
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rich.console import Console
from rich.panel import Panel

console = Console()


def _load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        try:
            return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    return {}


def _require(pkg: str) -> None:
    try:
        __import__(pkg.replace("-", "_"))
    except ImportError:
        console.print(
            f"[red]Pacote ausente:[/red] [bold]{pkg}[/bold]  "
            f"→ execute  [cyan]pip install -r requirements.txt[/cyan]"
        )
        sys.exit(1)


for _pkg in ("rich", "questionary", "yaml"):
    _require(_pkg)

import questionary
import yaml

from modules.context import (
    ReleaseContext,
    branch_from_version,
    increment_crm_version,
    increment_version,
    version_from_branch,
)
from modules.steps import ReleaseSteps
from modules import ui


# ---------------------------------------------------------------------------
# Prompts helpers
# ---------------------------------------------------------------------------


def _ask(prompt: str, default: str = "") -> str:
    if not sys.stdin.isatty():
        return default
    value = questionary.text(prompt, default=default).ask()
    if value is None:
        console.print("\n[yellow]Operação cancelada pelo usuário.[/yellow]")
        sys.exit(0)
    return value.strip()


def _confirm(prompt: str, default: bool = True) -> bool:
    if not sys.stdin.isatty():
        return default
    answer = questionary.confirm(prompt, default=default).ask()
    if answer is None:
        sys.exit(0)
    return answer


# ---------------------------------------------------------------------------
# Auto-detection
# ---------------------------------------------------------------------------


def _read_package_json(repo: str) -> dict:
    pkg_path = Path(repo) / "package.json"
    if pkg_path.exists():
        try:
            return json.loads(pkg_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _git_current_branch(repo: str) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def detect_context(repo: str) -> dict:
    """
    Lê package.json e branch atual para pré-preencher todos os campos.
    Retorna um dict com os valores detectados (podem ser vazios).
    """
    pkg = _read_package_json(repo)
    branch = _git_current_branch(repo)

    # Versão atual: prioriza package.json, fallback para branch
    pkg_version = pkg.get("version", "")
    branch_version = version_from_branch(branch) if branch.startswith("release/") else ""
    current_version = pkg_version or branch_version

    # crmVersion atual (do package.json)
    current_crm = str(pkg.get("crmVersion", ""))

    # Branch de release: usa a atual se já for release/, senão deriva do package.json
    if branch.startswith("release/"):
        current_release = branch
    else:
        current_release = branch_from_version(current_version) if current_version else branch

    # Próxima versão (incremento automático do minor)
    next_ver = increment_version(current_version) if current_version else ""
    next_crm = increment_crm_version(current_crm) if current_crm else ""
    next_branch = branch_from_version(next_ver) if next_ver else ""

    return {
        "current_branch": current_release,
        "current_version": current_version,
        "current_crm": current_crm,
        "next_version": next_ver,
        "next_crm": next_crm,
        "next_branch": next_branch,
    }


# ---------------------------------------------------------------------------
# Input collection — mostra o que foi detectado e pede confirmação
# ---------------------------------------------------------------------------


def collect_inputs(args: argparse.Namespace) -> ReleaseContext:
    repo = args.repo
    ctx = ReleaseContext()
    if args.docker_registry:
        ctx.docker_registry = args.docker_registry

    detected = detect_context(repo)

    ui.section("Detecção automática")

    if detected["current_branch"]:
        ui.success(f"Branch de release detectada: {detected['current_branch']}")
    if detected["current_version"]:
        ui.success(f"Versão atual (package.json): {detected['current_version']}")
    if detected["current_crm"]:
        ui.success(f"crmVersion atual (package.json): {detected['current_crm']}")

    console.print()
    ui.section("Confirme ou ajuste os valores")

    # --- Release atual ---
    ctx.current_release = args.current_release or _ask(
        "Branch da release atual",
        default=detected["current_branch"],
    )

    # Versão frontend: lida do package.json, confirmada pelo usuário
    ctx.frontend_version = args.frontend_version or _ask(
        "Versão frontend  (é o version do package.json — será usada para a tag Git e imagem Docker)",
        default=detected["current_version"] or version_from_branch(ctx.current_release),
    )

    # crmVersion ATUAL: lida do package.json
    ctx.crm_version = args.crm_version or _ask(
        "crmVersion ATUAL  (versão da API CRM já no package.json — usada só para validação)",
        default=detected["current_crm"] or "",
    )

    # --- Próxima release ---
    ctx.next_release = args.next_release or _ask(
        "Branch da PRÓXIMA release  (incremento automático, confirme ou ajuste)",
        default=detected["next_branch"] or "",
    )

    # crmVersion da PRÓXIMA release
    ctx.next_crm_version = args.next_crm_version or _ask(
        "crmVersion da PRÓXIMA release  (será gravada no package.json da nova branch)",
        default=detected["next_crm"] or "",
    )

    # --- Branches para merge ---
    if args.branches:
        ctx.branch_names = args.branches
    else:
        raw = _ask(
            "Branches para merge (separadas por vírgula, ou vazio para buscar via Slack)",
            default="",
        )
        if raw:
            ctx.branch_names = [b.strip() for b in raw.split(",") if b.strip()]

    return ctx


def collect_actions() -> dict:
    console.print()
    ui.section("Ações a executar")

    close   = _confirm("Fechar release atual (changelog + tag + develop/master)?", default=True)
    merge   = close and _confirm("Fazer merge das branches? (não se já fez manualmente)", default=True)
    image   = _confirm("Gerar imagem Docker?", default=True)
    publish = _confirm("Publicar imagens no Docker Hub?", default=True)
    nxt     = _confirm("Criar próxima release?", default=True)

    return {
        "close": close,
        "merge": merge,
        "image": image,
        "publish": publish,
        "next": nxt,
        "rc": nxt and image,
    }


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    config = _load_config()
    default_repo = config.get("repo_path") or "."

    p = argparse.ArgumentParser(
        prog="release.py",
        description="ZCRM Release Automation — Fechamento de Release",
    )
    p.add_argument(
        "--repo",
        default=default_repo,
        help=f"Caminho para o repositório frontend (padrão: {default_repo})",
    )
    p.add_argument("--current-release", default="", metavar="BRANCH")
    p.add_argument("--next-release", default="", metavar="BRANCH")
    p.add_argument("--frontend-version", default="", metavar="VERSION")
    p.add_argument("--crm-version", default="", metavar="VERSION",
                   help="crmVersion ATUAL (do package.json — apenas validação)")
    p.add_argument("--next-crm-version", default="", metavar="VERSION",
                   help="crmVersion da PRÓXIMA release (será gravada no package.json)")
    p.add_argument(
        "--branches",
        nargs="*",
        metavar="BRANCH",
        help="Lista de branches a fazer merge",
    )
    p.add_argument("--docker-registry", default="", metavar="REGISTRY")
    p.add_argument(
        "--yes",
        action="store_true",
        help="Aceitar todas as confirmações (CI mode)",
    )
    p.add_argument(
        "--resume-from-step",
        type=int,
        default=0,
        metavar="STEP",
        help="Retomar a partir de uma etapa específica (requer checkpoint em /tmp/release_checkpoint.json)",
    )
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # ── Resume mode: restore state from checkpoint and skip completed steps ──
    if args.resume_from_step:
        cp_file = Path("/tmp/release_checkpoint.json")
        if not cp_file.exists():
            console.print(
                "[red]Checkpoint não encontrado:[/red] /tmp/release_checkpoint.json\n"
                "Inicie uma nova execução completa da pipeline."
            )
            sys.exit(1)

        cp = json.loads(cp_file.read_text(encoding="utf-8"))
        ui.welcome_banner()
        ui.info(f"Retomando a partir da etapa {args.resume_from_step:02d}…")

        executor = ReleaseSteps.restore_from_checkpoint(cp, console, repo_path=args.repo)
        actions  = cp.get("actions", {"close": True, "merge": True, "image": True,
                                       "publish": True, "next": True, "rc": True})
        executor.run(actions, resume_from_step=args.resume_from_step)
        return

    # ── Normal mode ──────────────────────────────────────────────────────────
    ui.welcome_banner()

    ctx = collect_inputs(args)

    if args.yes:
        actions = {"close": True, "merge": True, "image": True, "publish": True, "next": True, "rc": True}
    else:
        actions = collect_actions()

    console.print()
    console.print(
        Panel(
            f"  Release atual   : [cyan]{ctx.current_release}[/cyan]\n"
            f"  Versão frontend : [cyan]{ctx.frontend_version}[/cyan]\n"
            f"  crmVersion atual: [cyan]{ctx.crm_version}[/cyan]\n"
            f"  Próxima release : [cyan]{ctx.next_release}[/cyan]\n"
            f"  crmVersion nova : [cyan]{ctx.next_crm_version}[/cyan]\n"
            f"  Docker tag      : [cyan]{ctx.docker_tag}[/cyan]\n"
            f"  Docker RC tag   : [cyan]{ctx.docker_tag_rc}[/cyan]\n"
            f"  Branches        : [cyan]{len(ctx.branches)} fornecida(s)[/cyan]"
            if ctx.branch_names else
            f"  Release atual   : [cyan]{ctx.current_release}[/cyan]\n"
            f"  Versão frontend : [cyan]{ctx.frontend_version}[/cyan]\n"
            f"  crmVersion atual: [cyan]{ctx.crm_version}[/cyan]\n"
            f"  Próxima release : [cyan]{ctx.next_release}[/cyan]\n"
            f"  crmVersion nova : [cyan]{ctx.next_crm_version}[/cyan]\n"
            f"  Docker tag      : [cyan]{ctx.docker_tag}[/cyan]\n"
            f"  Docker RC tag   : [cyan]{ctx.docker_tag_rc}[/cyan]\n"
            f"  Branches        : [yellow]buscar aprovadas no Slack[/yellow]",
            title="Resumo",
            border_style="cyan",
        )
    )

    console.print()
    if not args.yes and not _confirm("Confirmar e iniciar?", default=True):
        console.print("[yellow]Operação cancelada.[/yellow]")
        sys.exit(0)

    executor = ReleaseSteps(ctx, console, repo_path=args.repo, step_by_step=not args.yes)
    executor.run(actions)


if __name__ == "__main__":
    main()
