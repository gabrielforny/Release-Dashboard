"""
Final report — generates release-report.md.
"""

from datetime import datetime
from pathlib import Path

from modules.context import ReleaseContext, StepStatus


def generate(ctx: ReleaseContext, output_path: str = "release-report.md") -> str:
    elapsed = (datetime.now() - ctx.start_time).total_seconds()
    minutes, seconds = divmod(int(elapsed), 60)

    failed = [r for r in ctx.step_results if r.status == StepStatus.FAILED]
    overall = "SUCESSO" if not failed else "FALHOU"

    lines = [
        "# Release Report — ZCRM",
        "",
        f"**Data:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Status geral:** {overall}",
        "",
        "---",
        "",
        "## Informações da Release",
        "",
        f"| Campo            | Valor |",
        f"|------------------|-------|",
        f"| Release atual    | `{ctx.current_release}` |",
        f"| Próxima release  | `{ctx.next_release}` |",
        f"| Versão frontend  | `{ctx.frontend_version}` |",
        f"| crmVersion       | `{ctx.crm_version}` |",
        f"| Imagem Docker    | `{ctx.docker_tag}` |",
        f"| Imagem RC        | `{ctx.docker_tag_rc}` |",
        f"| Tempo total      | {minutes}m {seconds}s |",
        "",
        "---",
        "",
        "## Branches Aplicadas",
        "",
    ]

    if ctx.merged_branches:
        for b in ctx.merged_branches:
            lines.append(f"- ✅ `{b}`")
    else:
        lines.append("_Nenhuma branch aplicada._")

    if ctx.failed_branches:
        lines += ["", "## Branches com Conflito", ""]
        for b in ctx.failed_branches:
            lines.append(f"- ❌ `{b}`")

    lines += [
        "",
        "---",
        "",
        "## Etapas Executadas",
        "",
        "| Etapa | Status | Duração |",
        "|-------|--------|---------|",
    ]

    icons = {
        StepStatus.SUCCESS: "✅",
        StepStatus.FAILED: "❌",
        StepStatus.SKIPPED: "⏭",
        StepStatus.PENDING: "⏳",
        StepStatus.RUNNING: "🔄",
    }

    for r in ctx.step_results:
        icon = icons.get(r.status, "?")
        dur = f"{r.duration:.1f}s" if r.duration else "—"
        lines.append(f"| {r.name} | {icon} | {dur} |")

    if failed:
        lines += ["", "---", "", "## Erros", ""]
        for r in failed:
            lines += [
                f"### {r.name}",
                "",
                "```",
                r.error[:2000],
                "```",
                "",
            ]

    content = "\n".join(lines) + "\n"
    Path(output_path).write_text(content, encoding="utf-8")
    return output_path


def generate_hotfix(ctx, output_path: str = "hotfix-report.md") -> str:
    """Gera o relatório final do pipeline de hotfix (mesmo padrão de generate())."""
    elapsed = (datetime.now() - ctx.start_time).total_seconds()
    minutes, seconds = divmod(int(elapsed), 60)

    failed = [r for r in ctx.step_results if r.status == StepStatus.FAILED]
    overall = "SUCESSO" if not failed else "FALHOU"

    lines = [
        "# Hotfix Report — ZCRM",
        "",
        f"**Data:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"**Status geral:** {overall}",
        "",
        "---",
        "",
        "## Informações do Hotfix",
        "",
        f"| Campo                | Valor |",
        f"|-----------------------|-------|",
        f"| Branch de hotfix      | `{ctx.hotfix_branch}` |",
        f"| Ticket                | `{ctx.ticket}` |",
        f"| Release alvo          | `{ctx.target_release_branch}` |",
        f"| Versão do hotfix      | `{ctx.hotfix_version}` |",
        f"| Imagem Docker         | `{ctx.docker_tag}` |",
        f"| Release anterior?     | {'sim' if ctx.is_previous_release else 'não'} |",
    ]
    if ctx.is_previous_release:
        lines.append(f"| Release atual (propagação) | `{ctx.current_release_branch}` |")
    lines += [
        f"| Tempo total           | {minutes}m {seconds}s |",
        "",
        "---",
        "",
        "## Etapas Executadas",
        "",
        "| Etapa | Status | Duração |",
        "|-------|--------|---------|",
    ]

    icons = {
        StepStatus.SUCCESS: "✅",
        StepStatus.FAILED: "❌",
        StepStatus.SKIPPED: "⏭",
        StepStatus.PENDING: "⏳",
        StepStatus.RUNNING: "🔄",
    }

    for r in ctx.step_results:
        icon = icons.get(r.status, "?")
        dur = f"{r.duration:.1f}s" if r.duration else "—"
        lines.append(f"| {r.name} | {icon} | {dur} |")

    if failed:
        lines += ["", "---", "", "## Erros", ""]
        for r in failed:
            lines += [
                f"### {r.name}",
                "",
                "```",
                r.error[:2000],
                "```",
                "",
            ]

    content = "\n".join(lines) + "\n"
    Path(output_path).write_text(content, encoding="utf-8")
    return output_path
