"""
Rich UI helpers — banners, step headers, status rows, etc.
"""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

console = Console()

BRAND = "[bold cyan]ZCRM[/bold cyan] [white]Release Automation[/white]"


def welcome_banner(
    title: str = "Release Automation",
    subtitle: str = "Automação de Fechamento de Release",
) -> None:
    text = Text()
    text.append("  ZCRM  ", style="bold white on blue")
    text.append(f"  {title}", style="bold cyan")

    console.print()
    console.print(
        Panel(
            text,
            subtitle=f"[dim]{subtitle}[/dim]",
            border_style="cyan",
            padding=(1, 4),
        )
    )
    console.print()


def step_header(number: int, title: str) -> None:
    console.print(f"\n[bold cyan]▶  Etapa {number:02d}[/bold cyan]  [bold white]{title}[/bold white]")
    console.rule(style="dim")


def success(msg: str) -> None:
    console.print(f"[bold green]  ✓[/bold green]  {msg}")


def warning(msg: str) -> None:
    console.print(f"[bold yellow]  ⚠[/bold yellow]  {msg}")


def error(msg: str) -> None:
    console.print(f"[bold red]  ✗[/bold red]  {msg}")


def info(msg: str) -> None:
    console.print(f"[dim]  →[/dim]  {msg}")


def section(title: str) -> None:
    console.print(f"\n[bold white]{title}[/bold white]")


def branch_list(branches: list[str]) -> None:
    for b in branches:
        console.print(f"    [cyan]•[/cyan] {b}")


def summary_table(step_results: list) -> None:
    from modules.context import StepStatus

    table = Table(box=box.ROUNDED, border_style="dim", show_header=True, header_style="bold")
    table.add_column("Etapa", style="dim", width=32)
    table.add_column("Status", justify="center", width=10)
    table.add_column("Duração", justify="right", width=10)

    icons = {
        StepStatus.SUCCESS: "[green]✓[/green]",
        StepStatus.FAILED: "[red]✗[/red]",
        StepStatus.SKIPPED: "[dim]—[/dim]",
        StepStatus.PENDING: "[dim]?[/dim]",
        StepStatus.RUNNING: "[yellow]…[/yellow]",
    }

    for r in step_results:
        icon = icons.get(r.status, "?")
        dur = f"{r.duration:.1f}s" if r.duration else "—"
        table.add_row(r.name, icon, dur)

    console.print(table)
