#!/usr/bin/env python3
"""
ZCRM Hotfix Automation — entry point.

Automatiza o fluxo de hotfix descrito na seção "Geração de hotfix" do
processo de release:
  1. Merge da branch de hotfix na branch de release associada (_RC)
  2. Atualização de changelog + package.json (versão de patch) em commit único
  3. ng build --prod
  4. Tag + push do hotfix
  5. Se o hotfix for de uma release ANTERIOR à que está em teste: merge de
     propagação para a release atual, mantendo a versão do package.json da
     release atual em caso de conflito
  6. Build e push da imagem Docker do hotfix

Usage:
    # Roda de dentro do repositório frontend (auto-detecta a última tag):
    python hotfix.py

    # Ou apontando para o repo, já informando tudo (CI mode):
    python hotfix.py --repo /path/to/frontend --yes \\
        --hotfix-branch hotfix/NEOSDEV-1234-corrige-login \\
        --target-release release/v2.30.0_zcrm_api_2.30.0_RC \\
        --hotfix-version v2.30.4_zcrm_api_2.30.0 \\
        --previous --current-release release/v2.31.0_zcrm_api_2.31.0_RC
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from rich.console import Console
from rich.panel import Panel

console = Console()


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

from modules import changelog, git_ops
from modules.context import (
    HotfixContext,
    increment_patch_version,
    release_branch_for_version,
)
from modules.hotfix_steps import HotfixSteps
from modules import ui


# ---------------------------------------------------------------------------
# Prompts helpers
# ---------------------------------------------------------------------------


def _load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        try:
            return yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return {}
    return {}


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


def detect_defaults(repo: str) -> dict:
    """Sugestões a partir da última tag e da branch atual do repositório."""
    try:
        latest = git_ops.latest_tag(cwd=repo)
    except git_ops.GitError:
        latest = ""

    try:
        branch = git_ops.current_branch(cwd=repo)
    except git_ops.GitError:
        branch = ""

    return {
        "latest_tag": latest,
        "hotfix_branch": branch if branch.startswith("hotfix/") else "",
        "suggested_version": increment_patch_version(latest) if latest else "",
        "suggested_target": release_branch_for_version(latest) if latest else "",
    }


def collect_inputs(args: argparse.Namespace) -> HotfixContext:
    repo = args.repo
    ctx = HotfixContext()
    if args.docker_registry:
        ctx.docker_registry = args.docker_registry

    detected = detect_defaults(repo)

    ui.section("Detecção automática")
    if detected["latest_tag"]:
        ui.success(f"Última tag encontrada: {detected['latest_tag']}")
    if detected["hotfix_branch"]:
        ui.success(f"Branch de hotfix detectada: {detected['hotfix_branch']}")

    console.print()
    ui.section("Confirme ou ajuste os valores")

    ctx.hotfix_branch = args.hotfix_branch or _ask(
        "Branch de hotfix (já formalizada no canal, com a correção finalizada)",
        default=detected["hotfix_branch"],
    )

    ctx.target_release_branch = args.target_release or _ask(
        "Branch de release onde o hotfix entra primeiro (a release do bug)",
        default=detected["suggested_target"],
    )

    ctx.hotfix_version = args.hotfix_version or _ask(
        "Versão do hotfix (vira a tag e o version do package.json)",
        default=detected["suggested_version"],
    )

    ctx.ticket = args.ticket or _ask(
        "Chave do JIRA do hotfix (ex: NEOSDEV-1234; vazio para detectar da branch)",
        default=changelog.extract_ticket(ctx.hotfix_branch) or "",
    )

    ctx.description = args.description or _ask(
        "Descrição do hotfix para o CHANGELOG (vazio para gerar a partir da branch)",
        default=changelog.branch_to_description(ctx.hotfix_branch) if ctx.hotfix_branch else "",
    )

    ctx.is_previous_release = args.previous or _confirm(
        "Este hotfix é para uma release ANTERIOR à release atualmente em teste "
        "(precisa propagar o fix para a release atual)?",
        default=False,
    )

    if ctx.is_previous_release:
        ctx.current_release_branch = args.current_release or _ask(
            "Branch da release ATUAL (que vai receber o hotfix propagado)",
        )

    return ctx


def build_parser() -> argparse.ArgumentParser:
    config = _load_config()
    default_repo = config.get("repo_path") or "."

    p = argparse.ArgumentParser(
        prog="hotfix.py",
        description="ZCRM Hotfix Automation — Geração e propagação de hotfix",
    )
    p.add_argument(
        "--repo",
        default=default_repo,
        help=f"Caminho para o repositório frontend (padrão: {default_repo})",
    )
    p.add_argument("--hotfix-branch", default="", metavar="BRANCH")
    p.add_argument("--target-release", default="", metavar="BRANCH",
                   help="Branch de release onde o hotfix entra primeiro")
    p.add_argument("--hotfix-version", default="", metavar="VERSION")
    p.add_argument("--ticket", default="", metavar="TICKET")
    p.add_argument("--description", default="", metavar="TEXT")
    p.add_argument(
        "--previous",
        action="store_true",
        help="Hotfix é de uma release anterior (requer --current-release para propagação)",
    )
    p.add_argument("--current-release", default="", metavar="BRANCH")
    p.add_argument("--docker-registry", default="", metavar="REGISTRY")
    p.add_argument("--yes", action="store_true", help="Aceitar todas as confirmações (CI mode)")
    p.add_argument(
        "--resume-from-step",
        type=int,
        default=0,
        metavar="STEP",
        help="Retomar a partir de uma etapa específica (requer checkpoint em /tmp/hotfix_checkpoint.json)",
    )
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.resume_from_step:
        cp_file = Path("/tmp/hotfix_checkpoint.json")
        if not cp_file.exists():
            console.print(
                "[red]Checkpoint não encontrado:[/red] /tmp/hotfix_checkpoint.json\n"
                "Inicie uma nova execução completa do hotfix."
            )
            sys.exit(1)

        cp = json.loads(cp_file.read_text(encoding="utf-8"))
        ui.welcome_banner(title="Hotfix Automation", subtitle="Automação de Geração de Hotfix")
        ui.info(f"Retomando hotfix a partir da etapa {args.resume_from_step:02d}…")

        executor = HotfixSteps.restore_from_checkpoint(cp, console, repo_path=args.repo)
        executor.run(resume_from_step=args.resume_from_step)
        return

    ui.welcome_banner(title="Hotfix Automation", subtitle="Automação de Geração de Hotfix")

    ctx = collect_inputs(args)

    console.print()
    summary = (
        f"  Branch hotfix     : [cyan]{ctx.hotfix_branch}[/cyan]\n"
        f"  Release alvo      : [cyan]{ctx.target_release_branch}[/cyan]\n"
        f"  Versão do hotfix  : [cyan]{ctx.hotfix_version}[/cyan]\n"
        f"  Ticket            : [cyan]{ctx.ticket}[/cyan]\n"
        f"  Release anterior? : [cyan]{'sim' if ctx.is_previous_release else 'não'}[/cyan]\n"
    )
    if ctx.is_previous_release:
        summary += f"  Release atual     : [cyan]{ctx.current_release_branch}[/cyan]\n"

    console.print(Panel(summary, title="Resumo do hotfix", border_style="cyan"))

    if not args.yes and not _confirm("Confirma o início do pipeline de hotfix?", default=True):
        console.print("[yellow]Cancelado pelo usuário.[/yellow]")
        return

    step_by_step = not args.yes
    executor = HotfixSteps(ctx, console, repo_path=args.repo, step_by_step=step_by_step)
    executor.run()


if __name__ == "__main__":
    main()
