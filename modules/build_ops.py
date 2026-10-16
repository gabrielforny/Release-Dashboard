"""
Build operations — wrapper para `ng build --prod`, compartilhado entre o
pipeline de fechamento de release e o pipeline de hotfix.
"""

import subprocess
from pathlib import Path


class BuildError(Exception):
    pass


def ng_build_prod(cwd: str, timeout: int = 900, log_filename: str = "build.log") -> str:
    """
    Executa `npx ng build --prod` em 'cwd', salva stdout+stderr em 'log_filename'
    (dentro de cwd) e retorna o caminho do log.
    Lança BuildError se o build falhar (returncode != 0).
    """
    log_path = Path(cwd) / log_filename
    result = subprocess.run(
        ["npx", "ng", "build", "--prod"],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    log_path.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")

    if result.returncode != 0:
        raise BuildError(f"ng build --prod falhou — veja {log_path}\n{result.stderr[-2000:]}")

    return str(log_path)
