"""
Build operations — wrapper para `ng build --prod`, compartilhado entre o
pipeline de fechamento de release e o pipeline de hotfix.
"""

import subprocess
import time
from pathlib import Path


class BuildError(Exception):
    pass


class BuildSkipped(Exception):
    """Raised when the build is intentionally skipped via signal file."""
    pass


# Signal file path: dashboard writes this to request a mid-build skip
SKIP_SIGNAL_FILE = Path("/tmp/hotfix_skip_step6")


def ng_build_prod(cwd: str, timeout: int = 900, log_filename: str = "build.log") -> str:
    """
    Executa `npx ng build --prod` em 'cwd', salva stdout+stderr em 'log_filename'
    (dentro de cwd) e retorna o caminho do log.
    Lança BuildError se o build falhar, BuildSkipped se pulado via sinal.
    """
    log_path = Path(cwd) / log_filename

    # Clean up any stale skip signal from a previous run
    try:
        SKIP_SIGNAL_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    log_file = open(str(log_path), "w", encoding="utf-8")
    proc = subprocess.Popen(
        ["npx", "ng", "build", "--prod"],
        cwd=cwd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    log_file.close()  # Python closes its handle; proc's fd stays open until it exits

    start = time.time()
    try:
        while proc.poll() is None:
            if time.time() - start > timeout:
                proc.kill()
                proc.wait()
                raise BuildError(f"ng build --prod excedeu o timeout de {timeout}s")
            if SKIP_SIGNAL_FILE.exists():
                try:
                    SKIP_SIGNAL_FILE.unlink()
                except OSError:
                    pass
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                raise BuildSkipped("Build pulado pelo usuário via dashboard")
            time.sleep(2)
    except (BuildError, BuildSkipped):
        raise
    except Exception as e:
        proc.kill()
        proc.wait()
        raise BuildError(f"Erro inesperado durante ng build: {e}")

    if proc.returncode != 0:
        try:
            error_tail = log_path.read_text(encoding="utf-8")[-2000:]
        except Exception:
            error_tail = ""
        raise BuildError(f"ng build --prod falhou — veja {log_path}\n{error_tail}")

    return str(log_path)
