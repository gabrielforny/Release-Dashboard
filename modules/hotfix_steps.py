"""
Implementação do fluxo de hotfix (ver seção "Geração de hotfix" do processo
de release):

  1. Validação do ambiente
  2. Checkout + pull da branch de release onde o hotfix entra primeiro
  3. Merge da branch de hotfix nessa branch
  4. Atualizar CHANGELOG com o conteúdo do hotfix (chave JIRA + descrição)
  5. Atualizar package.json com a versão do hotfix + commit único + push
     ("changelog vX + versão atualizada no package.json")
  6. ng build --prod
  7. Criar e enviar a tag do hotfix
  8. Se o hotfix for de uma release ANTERIOR à que está em teste: propagar o
     fix para a branch da release atual, resolvendo o conflito de
     package.json mantendo a versão da release atual (--ours)
  9. Build e push da imagem Docker do hotfix
  10. Relatório final
"""

import json
import sys
import time
from pathlib import Path

from rich.console import Console

from modules import build_ops, changelog, docker_ops, git_ops
from modules.context import HotfixContext, StepResult, StepStatus
from modules import ui


def _read_crm_version(pkg_path: Path) -> str:
    try:
        pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
        return str(pkg.get("crmVersion", ""))
    except Exception:
        return ""


class HotfixSteps:
    _CHECKPOINT_FILE = Path("/tmp/hotfix_checkpoint.json")
    _CONFLICT_PAUSE_FILE = Path("/tmp/hotfix_conflict_pause.json")
    _CONFLICT_RESUME_FILE = Path("/tmp/hotfix_conflict_resume")

    def __init__(
        self,
        ctx: HotfixContext,
        console: Console,
        repo_path: str = ".",
        step_by_step: bool = False,
    ):
        self.ctx = ctx
        self.console = console
        self.repo = repo_path
        self.step_by_step = step_by_step

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _step(self, name: str):
        return _StepContext(self.ctx, name, self.console)

    def _pause(self, prompt: str = "Continuar para o próximo passo?") -> bool:
        if not self.step_by_step or not sys.stdin.isatty():
            return True
        import questionary

        self.console.print()
        answer = questionary.confirm(prompt, default=True).ask()
        if not answer:
            self.console.print("[yellow]Pipeline pausado pelo usuário.[/yellow]")
        return bool(answer)

    def _wait_for_conflict_resolution(self, step, branches: list[str]) -> bool:
        """
        Sinaliza o dashboard (CONFLICT_PAUSE|...) e aguarda confirmação de
        resolução manual via /tmp/hotfix_conflict_resume (até 2h).
        Retorna True se resolvido, False em timeout.
        """
        print(f"CONFLICT_PAUSE|{'|'.join(branches)}", flush=True)
        ui.error(f"Conflito ao mesclar {', '.join(branches)}")
        ui.info("Resolva manualmente no repositório e confirme no dashboard.")

        self._CONFLICT_PAUSE_FILE.write_text(
            json.dumps({"conflicts": branches}), encoding="utf-8"
        )

        for _ in range(3600):
            if self._CONFLICT_RESUME_FILE.exists():
                try:
                    self._CONFLICT_RESUME_FILE.unlink()
                except OSError:
                    pass
                try:
                    self._CONFLICT_PAUSE_FILE.unlink()
                except OSError:
                    pass
                return True
            time.sleep(2)

        try:
            self._CONFLICT_PAUSE_FILE.unlink()
        except OSError:
            pass
        step.fail("Timeout: conflito não resolvido em 2 horas")
        return False

    # ------------------------------------------------------------------
    # Etapa 1 — Validação do ambiente
    # ------------------------------------------------------------------

    def step_01_validate_environment(self) -> bool:
        ui.step_header(1, "Validação do ambiente")
        with self._step("Validação do ambiente") as step:
            errors: list[str] = []

            git_user = git_ops.user_name(cwd=self.repo)
            if not git_user:
                errors.append("git user.name não configurado")
            else:
                ui.success(f"Git user: {git_user}")

            dirty = git_ops.status(cwd=self.repo)
            if dirty:
                errors.append(f"Árvore de trabalho suja:\n{dirty}")
            else:
                ui.success("Árvore de trabalho limpa")

            if not docker_ops.is_logged_in():
                errors.append("Docker não autenticado — execute 'docker login'")
            else:
                ui.success("Docker autenticado")

            if not self.ctx.hotfix_branch:
                errors.append("Branch de hotfix não informada")
            if not self.ctx.target_release_branch:
                errors.append("Branch de release alvo não informada")
            if not self.ctx.hotfix_version:
                errors.append("Versão do hotfix não informada")
            if self.ctx.is_previous_release and not self.ctx.current_release_branch:
                errors.append(
                    "Hotfix de release anterior requer a branch da release atual "
                    "(para propagação)"
                )

            if errors:
                for e in errors:
                    ui.error(e)
                step.fail("\n".join(errors))
                return False

            step.ok()
            return True

    # ------------------------------------------------------------------
    # Etapa 2 — Checkout + pull da release alvo
    # ------------------------------------------------------------------

    def step_02_checkout_target(self) -> bool:
        ui.step_header(2, f"Checkout da release alvo ({self.ctx.target_release_branch})")
        with self._step("Checkout release alvo") as step:
            branch = self.ctx.target_release_branch
            try:
                git_ops.checkout(branch, cwd=self.repo)
                ui.success(f"Checkout: {branch}")

                try:
                    git_ops.pull(branch, cwd=self.repo)
                    ui.success("Pull concluído")
                except git_ops.GitError as e:
                    ui.warning(f"Pull falhou (rede indisponível?) — continuando com estado local: {e}")

                try:
                    git_ops.fetch_all(cwd=self.repo)
                    ui.success("Fetch concluído")
                except git_ops.GitError:
                    ui.warning("Fetch falhou — refs remotos podem estar desatualizados (sem rede)")

                step.ok()
                return True
            except git_ops.GitError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 3 — Merge da branch de hotfix
    # ------------------------------------------------------------------

    def step_03_merge_hotfix(self) -> bool:
        ui.step_header(3, f"Merge de {self.ctx.hotfix_branch}")
        with self._step("Merge do hotfix") as step:
            output, had_conflict = git_ops.merge(self.ctx.hotfix_branch, cwd=self.repo)
            if had_conflict:
                if not self._wait_for_conflict_resolution(step, [self.ctx.hotfix_branch]):
                    return False
                ui.success(f"Conflito resolvido manualmente: {self.ctx.hotfix_branch}")
            else:
                ui.success(f"Merged: {self.ctx.hotfix_branch}")

            step.ok()
            return True

    # ------------------------------------------------------------------
    # Etapa 4 — Atualizar CHANGELOG
    # ------------------------------------------------------------------

    def step_04_update_changelog(self) -> bool:
        ui.step_header(4, "Atualizar CHANGELOG")
        with self._step("Atualizar CHANGELOG") as step:
            entry = changelog.build_hotfix_entry(
                self.ctx.hotfix_branch, self.ctx.ticket, self.ctx.description
            )
            # Mantém os valores detectados/confirmados no contexto para o relatório final
            self.ctx.ticket = entry["ticket"]
            self.ctx.description = entry["description"]

            block = changelog.render_hotfix_block(self.ctx.hotfix_version, entry)
            changelog_path = Path(self.repo) / "CHANGELOG.md"
            changelog.prepend_to_changelog(changelog_path, block)

            ui.success(f"CHANGELOG atualizado: {changelog_path}")
            ui.info(f"Revise o arquivo em: {changelog_path}")

            if not self._pause("CHANGELOG ok — continuar para package.json?"):
                step.fail("Cancelado pelo usuário")
                return False

            step.ok()
            return True

    # ------------------------------------------------------------------
    # Etapa 5 — Atualizar package.json + commit único + push
    # ------------------------------------------------------------------

    def step_05_update_package_json(self) -> bool:
        ui.step_header(5, "Atualizar package.json + commit")
        with self._step("Atualizar package.json") as step:
            pkg_path = Path(self.repo) / "package.json"
            if not pkg_path.exists():
                ui.warning("package.json não encontrado — pulando atualização de versão")
                if not self._pause("Prosseguir sem atualizar package.json?"):
                    step.fail("Cancelado pelo usuário")
                    return False
                step.skip()
                return True

            try:
                crm_version = _read_crm_version(pkg_path)
                git_ops.set_version_in_package_json(
                    str(pkg_path), version=self.ctx.hotfix_version, crm_version=crm_version
                )
                ui.success(f"package.json atualizado: version={self.ctx.hotfix_version}")
                ui.info(f"Revise o arquivo em: {pkg_path}")

                if not self._pause("package.json ok — commitar e fazer push?"):
                    step.fail("Cancelado pelo usuário antes do commit")
                    return False

                git_ops.add(["CHANGELOG.md", "package.json"], cwd=self.repo)
                git_ops.commit(
                    f"changelog {self.ctx.hotfix_version} + versão atualizada no package.json",
                    cwd=self.repo,
                )
                git_ops.push(self.ctx.target_release_branch, cwd=self.repo)
                ui.success("Commit e push realizados")
                step.ok()
                return True
            except git_ops.GitError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 6 — Build
    # ------------------------------------------------------------------

    def step_06_build(self) -> bool:
        ui.step_header(6, "ng build --prod")
        with self._step("ng build --prod") as step:
            ui.info("Executando ng build --prod (pode demorar) …")
            try:
                log_path = build_ops.ng_build_prod(self.repo, timeout=900)
                self.ctx.build_log_path = log_path
                ui.success(f"Build concluído — log salvo em {log_path}")
                step.ok()
                return True
            except build_ops.BuildError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 7 — Criar e enviar tag do hotfix
    # ------------------------------------------------------------------

    def step_07_create_tag(self) -> bool:
        ui.step_header(7, "Criar tag do hotfix")
        with self._step("Criar tag do hotfix") as step:
            tag = self.ctx.hotfix_version
            try:
                git_ops.tag(tag, tag, cwd=self.repo)
                ui.success(f"Tag criada: {tag}")
                git_ops.push_tags(cwd=self.repo)
                ui.success("Tags enviadas para o remoto")
                step.ok()
                return True
            except git_ops.GitError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 8 — Propagar para a release atual (se for hotfix de release anterior)
    # ------------------------------------------------------------------

    def step_08_forward_merge(self) -> bool:
        ui.step_header(
            8, f"Propagar hotfix para a release atual ({self.ctx.current_release_branch or '—'})"
        )
        with self._step("Merge para a release atual") as step:
            if not self.ctx.is_previous_release:
                ui.info("Hotfix já é da release atual — nada a propagar")
                step.skip()
                return True

            try:
                git_ops.checkout(self.ctx.current_release_branch, cwd=self.repo)
                ui.success(f"Checkout: {self.ctx.current_release_branch}")

                try:
                    git_ops.pull(self.ctx.current_release_branch, cwd=self.repo)
                    ui.success("Pull concluído")
                except git_ops.GitError as e:
                    ui.warning(f"Pull falhou — continuando com estado local: {e}")

                output, had_conflict = git_ops.merge_keep_ours(
                    self.ctx.target_release_branch, ["package.json"], cwd=self.repo
                )
                if had_conflict:
                    if not self._wait_for_conflict_resolution(
                        step, [self.ctx.target_release_branch]
                    ):
                        return False
                    ui.success("Conflito resolvido manualmente")
                else:
                    ui.success(output)

                git_ops.push(self.ctx.current_release_branch, cwd=self.repo)
                ui.success(f"Push {self.ctx.current_release_branch}")
                step.ok()
                return True
            except git_ops.GitError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 9 — Build e push da imagem Docker do hotfix
    # ------------------------------------------------------------------

    def step_09_docker_build_push(self) -> bool:
        ui.step_header(9, "Build e push da imagem Docker do hotfix")
        with self._step("Docker build + push") as step:
            try:
                git_ops.checkout(self.ctx.target_release_branch, cwd=self.repo)
                ui.info("Executando make docker-file …")
                docker_ops.make_docker_file(cwd=self.repo, timeout=600)
                ui.success("Imagem Docker construída")

                tag = self.ctx.docker_tag
                docker_ops.tag_image("zcrm_front:latest", tag)
                ui.success(f"Tag criada: {tag}")

                ui.info("Push em andamento …")
                docker_ops.push_image(tag, timeout=300)
                ui.success(f"Push concluído: {tag}")
                step.ok()
                return True
            except (docker_ops.DockerError, git_ops.GitError) as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 10 — Relatório final
    # ------------------------------------------------------------------

    def step_10_final_report(self) -> bool:
        ui.step_header(10, "Relatório final")
        with self._step("Relatório final") as step:
            from modules import report as report_mod

            path = report_mod.generate_hotfix(self.ctx)
            ui.success(f"Relatório salvo em: {path}")
            step.ok()
            return True

    # ------------------------------------------------------------------
    # Checkpoint helpers
    # ------------------------------------------------------------------

    def _save_checkpoint(self, last_step: int) -> None:
        try:
            data = {
                "resume_from_step": last_step + 1,
                "hotfix_branch": self.ctx.hotfix_branch,
                "ticket": self.ctx.ticket,
                "description": self.ctx.description,
                "target_release_branch": self.ctx.target_release_branch,
                "hotfix_version": self.ctx.hotfix_version,
                "is_previous_release": self.ctx.is_previous_release,
                "current_release_branch": self.ctx.current_release_branch,
                "docker_registry": self.ctx.docker_registry,
                "repo": self.repo,
            }
            self._CHECKPOINT_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    @classmethod
    def restore_from_checkpoint(cls, cp: dict, console, repo_path: str = "."):
        ctx = HotfixContext()
        ctx.hotfix_branch = cp.get("hotfix_branch", "")
        ctx.ticket = cp.get("ticket", "")
        ctx.description = cp.get("description", "")
        ctx.target_release_branch = cp.get("target_release_branch", "")
        ctx.hotfix_version = cp.get("hotfix_version", "")
        ctx.is_previous_release = cp.get("is_previous_release", False)
        ctx.current_release_branch = cp.get("current_release_branch", "")
        ctx.docker_registry = cp.get("docker_registry", "zanthusinovacao1/zcrm_front")
        repo = cp.get("repo", repo_path) if repo_path == "." else repo_path
        return cls(ctx, console, repo_path=repo, step_by_step=False)

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    def run(self, resume_from_step: int = 0) -> None:
        self.console.print()

        pipeline = [
            self.step_01_validate_environment,
            self.step_02_checkout_target,
            self.step_03_merge_hotfix,
            self.step_04_update_changelog,
            self.step_05_update_package_json,
            self.step_06_build,
            self.step_07_create_tag,
            self.step_08_forward_merge,
            self.step_09_docker_build_push,
        ]
        report_step = self.step_10_final_report
        pipeline.append(report_step)

        for step_fn in pipeline:
            try:
                step_num = int(step_fn.__name__.split("_")[1])
            except (ValueError, IndexError):
                step_num = 0

            if resume_from_step and step_num and step_num < resume_from_step:
                ui.info(f"Pulando etapa {step_num:02d} (retomada a partir da etapa {resume_from_step})")
                continue

            try:
                ok = step_fn()
            except Exception as exc:
                ui.error(f"Exceção não tratada na etapa {step_num:02d}: {exc}")
                if step_num:
                    print(f"STEP_FAIL|{step_num}", flush=True)
                if step_fn is not report_step:
                    self.step_10_final_report()
                break

            if not ok:
                if step_num:
                    print(f"STEP_FAIL|{step_num}", flush=True)
                ui.error("Etapa falhou — interrompendo pipeline de hotfix. Veja o relatório para detalhes.")
                if step_fn is not report_step:
                    self.step_10_final_report()
                break

            if step_num and step_fn is not report_step:
                self._save_checkpoint(step_num)

        self.console.print()
        ui.section("Resumo das etapas")
        ui.summary_table(self.ctx.step_results)


# ------------------------------------------------------------------
# Internal step timing context manager
# ------------------------------------------------------------------


class _StepContext:
    def __init__(self, ctx: HotfixContext, name: str, console: Console):
        self._ctx = ctx
        self._name = name
        self._console = console
        self._result = StepResult(name=name, status=StepStatus.RUNNING)
        self._t0 = 0.0

    def __enter__(self):
        self._t0 = time.monotonic()
        return self

    def ok(self):
        self._result.status = StepStatus.SUCCESS

    def fail(self, error: str = ""):
        self._result.status = StepStatus.FAILED
        self._result.error = error

    def skip(self):
        self._result.status = StepStatus.SKIPPED

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._result.duration = time.monotonic() - self._t0
        if exc_type is not None:
            self._result.status = StepStatus.FAILED
            self._result.error = str(exc_val)
        self._ctx.record(self._result)
        return False
