"""
Implementação das 16 etapas do fechamento de release ZCRM.
Cada método retorna True (sucesso) ou False (falha) e registra o resultado no ctx.
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from modules import build_ops, changelog, docker_ops, git_ops
from modules.context import ReleaseContext, StepResult, StepStatus
from modules import ui


class ReleaseSteps:
    def __init__(self, ctx: ReleaseContext, console: Console, repo_path: str = ".", step_by_step: bool = False):
        self.ctx = ctx
        self.console = console
        self.repo = repo_path
        self.step_by_step = step_by_step
        self._starting_head: Optional[str] = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _step(self, name: str):
        """Context manager that times a step and records its result."""
        return _StepContext(self.ctx, name, self.console)

    def _git(self, *args, **kwargs):
        return dict(cwd=self.repo, **kwargs)

    def _rollback_release(self, branch: str, starting_head: str) -> None:
        """Reset the release branch to starting_head if no push has happened yet."""
        try:
            current = git_ops.current_branch(cwd=self.repo)
            if current != branch:
                git_ops.checkout(branch, cwd=self.repo)

            local_head = git_ops.rev_parse_head(cwd=self.repo)
            if local_head == starting_head:
                return  # nothing was committed

            try:
                remote_head = git_ops.remote_head(branch, cwd=self.repo)
            except git_ops.GitError:
                remote_head = None

            # Only rollback if remote hasn't advanced (push never succeeded)
            if remote_head and remote_head != starting_head:
                ui.warning(
                    f"Branch remota '{branch}' já foi atualizada — rollback não é seguro. "
                    f"Verifique o estado manualmente."
                )
                return

            git_ops.reset_hard(starting_head, cwd=self.repo)
            ui.warning(
                f"Rollback aplicado: branch '{branch}' restaurada para {starting_head[:8]} "
                f"(commits locais desfeitos)"
            )
        except git_ops.GitError as e:
            ui.error(f"Rollback falhou: {e}")

    def _pause(self, prompt: str = "Continuar para o próximo passo?") -> bool:
        if not self.step_by_step or not sys.stdin.isatty():
            return True
        import questionary
        self.console.print()
        answer = questionary.confirm(prompt, default=True).ask()
        if not answer:
            self.console.print("[yellow]Pipeline pausado pelo usuário.[/yellow]")
        return bool(answer)

    _FAIL_DECISION_FILE = Path("/tmp/release_fail_decision")

    def _wait_for_fail_decision(self) -> str:
        """
        Pausa a pipeline e aguarda decisão do dashboard (arquivo de sinalização).
        Retorna "skip" (pular etapa e continuar) ou "abort" (interromper).
        """
        self._FAIL_DECISION_FILE.unlink(missing_ok=True)
        # Em modo interativo (terminal) não tem dashboard, aborta direto
        if self.step_by_step:
            return "abort"
        print("WAITING_FAIL_DECISION", flush=True)
        for _ in range(600):   # até 10 min de espera
            time.sleep(1)
            if self._FAIL_DECISION_FILE.exists():
                try:
                    dec = self._FAIL_DECISION_FILE.read_text().strip().lower()
                    self._FAIL_DECISION_FILE.unlink(missing_ok=True)
                    return dec if dec in ("skip", "abort") else "abort"
                except Exception:
                    pass
        return "abort"

    # ------------------------------------------------------------------
    # Etapa 1 — Validação inicial
    # ------------------------------------------------------------------

    def step_01_validate_environment(self) -> bool:
        ui.step_header(1, "Validação do ambiente")
        with self._step("Validação do ambiente") as step:
            errors: list[str] = []

            # Usuário Git configurado
            git_user = git_ops.user_name(cwd=self.repo)
            if not git_user:
                errors.append("git user.name não configurado")
            else:
                ui.success(f"Git user: {git_user}")

            # Git status limpo (antes de qualquer checkout)
            dirty = git_ops.status(cwd=self.repo)
            if dirty:
                errors.append(f"Árvore de trabalho suja:\n{dirty}")
            else:
                ui.success("Árvore de trabalho limpa")

            # Docker autenticado
            if not docker_ops.is_logged_in():
                errors.append("Docker não autenticado — execute 'docker login'")
            else:
                ui.success("Docker autenticado")

            if errors:
                for err in errors:
                    ui.error(err)
                step.fail("\n".join(errors))
                return False

            step.ok()
            return True

    # ------------------------------------------------------------------
    # Etapa 2 — Checkout + pull da release (ponto de partida real)
    # ------------------------------------------------------------------

    def step_02_checkout_release(self) -> bool:
        ui.step_header(2, "Checkout da release atual")
        with self._step("Checkout release atual") as step:
            branch = self.ctx.current_release
            try:
                ui.info(f"Entrando na branch: {branch} …")
                git_ops.checkout(branch, cwd=self.repo)
                ui.success(f"Checkout: {branch}")

                ui.info("Sincronizando com origin (git pull) …")
                try:
                    git_ops.pull(branch, cwd=self.repo)
                    ui.success("Pull concluído")
                except git_ops.GitError as pull_err:
                    ui.warning(f"Pull falhou (rede indisponível?) — continuando com estado local: {pull_err}")

                # Fetch de todos os refs remotos (branches de features etc.)
                ui.info("Buscando refs remotos (git fetch --all) …")
                try:
                    git_ops.fetch_all(cwd=self.repo)
                    ui.success("Fetch concluído")

                    # Validar sincronização só se fetch funcionou
                    local = git_ops.rev_parse_head(cwd=self.repo)
                    remote = git_ops.remote_head(branch, cwd=self.repo)
                    if local != remote:
                        ui.warning(
                            f"HEAD local ({local[:8]}) difere do remoto ({remote[:8]}) "
                            f"— verifique a conexão e se a branch está atualizada"
                        )
                    else:
                        ui.success(f"HEAD sincronizado: {local[:8]}")
                except git_ops.GitError:
                    ui.warning("Fetch falhou — refs remotos podem estar desatualizados (sem rede)")
                step.ok()
                return True

            except git_ops.GitError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 3 — Coletar branches formalizadas
    # ------------------------------------------------------------------

    def step_03_collect_branches(self) -> bool:
        ui.step_header(3, "Branches formalizadas")
        with self._step("Coletar branches") as step:
            # Já foram fornecidas manualmente
            if self.ctx.branch_names or self.ctx.branch_entries:
                total = len(self.ctx.branches)
                ui.success(f"{total} branches fornecidas manualmente")
                ui.branch_list(self.ctx.branches)
                step.ok()
                return True

            # Buscar via Slack
            import os
            if not os.getenv("SLACK_TOKEN"):
                ui.warning("SLACK_TOKEN não definido — não é possível buscar branches via Slack.")
                ui.warning("Defina SLACK_TOKEN no ambiente ou passe as branches manualmente.")
                step.fail("Sem branches e sem SLACK_TOKEN")
                return False

            from modules import slack_ops

            ui.info(
                f"Buscando branches aprovadas para Release {self.ctx.crm_version} "
                f"/ {self.ctx.frontend_version} via Slack …"
            )
            try:
                entries = slack_ops.fetch_approved_branches(
                    crm_version=self.ctx.crm_version,
                    frontend_version=self.ctx.frontend_version,
                )
            except slack_ops.SlackError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

            # Diagnóstico: mostra o que foi lido mesmo que não tenha aprovadas
            diag = slack_ops.last_fetch_diag
            if diag:
                ui.info(
                    f"Slack: {diag['total_msgs']} mensagens lidas, "
                    f"{diag['total_formalizations']} formalizações encontradas"
                )

            if not entries:
                ui.warning(
                    f"Nenhuma branch aprovada encontrada.\n"
                    f"  O robô procura por respostas nas threads que contenham:\n"
                    f"    Release {self.ctx.crm_version}\n"
                    f"    {self.ctx.frontend_version}\n"
                    f"  Verifique se as respostas de aprovação estão no canal #team-frontend."
                )
                step.fail("Nenhuma branch aprovada encontrada")
                return False

            self.ctx.branch_entries = entries

            # Mostra as branches encontradas e pede confirmação antes de continuar
            self.console.print()
            ui.section(
                f"{len(entries)} branches aprovadas para Release "
                f"{self.ctx.crm_version} / {self.ctx.frontend_version}"
            )
            for i, e in enumerate(entries, 1):
                ticket = f"[{e['ticket']}] " if e.get("ticket") else ""
                formalized_at = slack_ops.ts_to_str(e.get("formalized_at", ""))
                data_str = f"(formalizada em {formalized_at}) " if formalized_at else ""
                ui.info(f"  {i:2d}. {data_str}{ticket}{e['branch']}")
                if e.get("description"):
                    ui.info(f"       → {e['description']}")
            self.console.print()

            if sys.stdin.isatty():
                import questionary
                confirmed = questionary.confirm(
                    f"Confirmar essas {len(entries)} branches para o fechamento?",
                    default=False,
                ).ask()
                if not confirmed:
                    step.fail("Fechamento cancelado pelo usuário na confirmação de branches")
                    return False

            step.ok()
            return True

    # ------------------------------------------------------------------
    # Etapa 4 — Merge automático
    # ------------------------------------------------------------------

    def step_04_merge_branches(self) -> bool:
        ui.step_header(4, "Merge das branches")
        with self._step("Merge das branches") as step:
            total = len(self.ctx.branches)
            conflicts: list[str] = []

            for i, branch in enumerate(self.ctx.branches, 1):
                ui.info(f"[{i}/{total}] Merging {branch} …")
                output, had_conflict = git_ops.merge(branch, cwd=self.repo)
                if had_conflict:
                    ui.error(f"Conflito em {branch}")
                    conflicts.append(branch)
                    self.ctx.failed_branches.append(branch)
                else:
                    ui.success(f"Merged: {branch}")
                    self.ctx.merged_branches.append(branch)

            if conflicts:
                # Signal the dashboard to show the conflict resolution UI
                print(f"CONFLICT_PAUSE|{'|'.join(conflicts)}", flush=True)
                ui.warning(
                    f"{len(conflicts)} branch(es) com conflito. "
                    "Resolva manualmente no repositório e confirme no dashboard."
                )
                ui.info("Aguardando confirmação de resolução no dashboard…")

                _pause_file  = Path("/tmp/release_conflict_pause.json")
                _resume_file = Path("/tmp/release_conflict_resume")
                _pause_file.write_text(
                    json.dumps({"conflicts": conflicts}), encoding="utf-8"
                )

                # Poll for the resume signal (max 2 h)
                for _ in range(3600):
                    if _resume_file.exists():
                        try:
                            _resume_file.unlink()
                        except OSError:
                            pass
                        try:
                            _pause_file.unlink()
                        except OSError:
                            pass
                        break
                    time.sleep(2)
                else:
                    try:
                        _pause_file.unlink()
                    except OSError:
                        pass
                    step.fail("Timeout: conflitos não resolvidos em 2 horas")
                    return False

                # User confirmed conflicts resolved — treat as merged
                ui.success(f"Conflitos resolvidos manualmente: {', '.join(conflicts)}")
                self.ctx.merged_branches.extend(conflicts)
                for b in conflicts:
                    if b in self.ctx.failed_branches:
                        self.ctx.failed_branches.remove(b)

            step.ok()
            return True

    # ------------------------------------------------------------------
    # Etapa 5 — Atualizar changelog
    # ------------------------------------------------------------------

    def step_05_update_changelog(self) -> bool:
        ui.step_header(5, "Atualizar CHANGELOG")
        with self._step("Atualizar CHANGELOG") as step:
            # Se veio do Slack, usa as descrições já extraídas
            if self.ctx.branch_entries:
                merged_set = set(self.ctx.merged_branches)
                if merged_set:
                    # Merge foi feito pelo pipeline — filtra só as que passaram
                    entries = [e for e in self.ctx.branch_entries if e["branch"] in merged_set]
                else:
                    # Merge foi feito manualmente — usa todas as branches do Slack
                    entries = list(self.ctx.branch_entries)
            else:
                entries = changelog.build_entries(self.ctx.merged_branches)
            self.ctx.changelog_entries = entries

            block = changelog.render_block(self.ctx.frontend_version, entries)
            changelog_path = Path(self.repo) / "CHANGELOG.md"

            changelog.prepend_to_changelog(changelog_path, block)
            ui.success(f"CHANGELOG atualizado: {changelog_path}")
            ui.info(f"Revise o arquivo em: {changelog_path}")

            if not self._pause("CHANGELOG ok — commitar e fazer push?"):
                step.fail("Cancelado pelo usuário antes do commit")
                return False

            try:
                git_ops.add(["CHANGELOG.md"], cwd=self.repo)
                git_ops.commit(
                    f"changelog {self.ctx.frontend_version}", cwd=self.repo
                )
                git_ops.push(self.ctx.current_release, cwd=self.repo)
                ui.success("Commit e push do changelog realizados")
                step.ok()
                return True
            except git_ops.GitError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 6 — Validar package.json
    # ------------------------------------------------------------------

    def step_06_validate_package_json(self) -> bool:
        ui.step_header(6, "Validar package.json")
        with self._step("Validar package.json") as step:
            pkg_path = Path(self.repo) / "package.json"
            if not pkg_path.exists():
                ui.warning("package.json não encontrado — pulando validação")
                step.skip()
                return True

            with open(pkg_path, encoding="utf-8") as f:
                pkg = json.load(f)

            version_ok = pkg.get("version") == self.ctx.frontend_version
            crm_ok = str(pkg.get("crmVersion", "")) == str(self.ctx.crm_version)

            if version_ok:
                ui.success(f"version: {pkg['version']}")
            else:
                ui.error(
                    f"version divergente — package.json: {pkg.get('version')} "
                    f"| esperado: {self.ctx.frontend_version}"
                )

            if crm_ok:
                ui.success(f"crmVersion: {pkg.get('crmVersion')}")
            else:
                ui.error(
                    f"crmVersion divergente — package.json: {pkg.get('crmVersion')} "
                    f"| esperado: {self.ctx.crm_version}"
                )

            if not (version_ok and crm_ok):
                step.fail("package.json divergente")
                return False

            step.ok()
            return True

    # ------------------------------------------------------------------
    # Etapa 7 — Build
    # ------------------------------------------------------------------

    def step_07_build(self) -> bool:
        ui.step_header(7, "ng build --prod")
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
    # Etapa 8 — Criar tag Git
    # ------------------------------------------------------------------

    def step_08_create_tag(self) -> bool:
        ui.step_header(8, "Criar tag Git")
        with self._step("Criar tag Git") as step:
            tag = self.ctx.frontend_version
            try:
                # Idempotente: se a tag já existe localmente, não tenta criar de novo
                existing = git_ops.list_tags(tag, cwd=self.repo)
                if existing:
                    ui.warning(f"Tag '{tag}' já existe localmente — ignorando criação")
                else:
                    git_ops.tag(tag, f"Release {tag}", cwd=self.repo)
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
    # Etapa 9 — Atualizar develop
    # ------------------------------------------------------------------

    def step_09_update_develop(self) -> bool:
        ui.step_header(9, "Merge release → develop")
        with self._step("Merge release → develop") as step:
            try:
                git_ops.checkout("develop", cwd=self.repo)
                ui.success("Checkout develop")

                output, conflict = git_ops.merge(
                    self.ctx.current_release, cwd=self.repo
                )
                if conflict:
                    step.fail(output)
                    return False

                ui.success(f"Merge de {self.ctx.current_release} em develop")

                git_ops.push("develop", cwd=self.repo)
                ui.success("Push develop")
                step.ok()
                return True
            except git_ops.GitError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 10 — Atualizar master
    # ------------------------------------------------------------------

    def step_10_update_master(self) -> bool:
        ui.step_header(10, "Merge develop → master")
        with self._step("Merge develop → master") as step:
            try:
                git_ops.checkout("master", cwd=self.repo)
                ui.success("Checkout master")

                output, conflict = git_ops.merge("develop", cwd=self.repo)
                if conflict:
                    step.fail(output)
                    return False

                ui.success("Merge de develop em master")

                git_ops.push("master", cwd=self.repo)
                ui.success("Push master")
                step.ok()
                return True
            except git_ops.GitError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 11 — Build Docker
    # ------------------------------------------------------------------

    def step_11_docker_build(self) -> bool:
        ui.step_header(11, "Build Docker (make docker-file)")
        with self._step("Build Docker") as step:
            try:
                git_ops.checkout(self.ctx.current_release, cwd=self.repo)
                ui.info("Executando make docker-file …")

                docker_ops.make_docker_file(cwd=self.repo, timeout=1200)
                ui.success("Imagem Docker construída")
                step.ok()
                return True
            except (docker_ops.DockerError, git_ops.GitError) as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 12 — Docker Hub push
    # ------------------------------------------------------------------

    def step_12_docker_push(self) -> bool:
        ui.step_header(12, "Docker Hub — tag e push")
        with self._step("Docker Hub push") as step:
            tag = self.ctx.docker_tag
            try:
                ui.info(f"Tag: {tag}")
                docker_ops.tag_image("zcrm_front:latest", tag)
                ui.success(f"Tag criada: {tag}")

                ui.info("Push em andamento …")
                docker_ops.push_image(tag, timeout=300)
                ui.success(f"Push concluído: {tag}")
                step.ok()
                return True
            except docker_ops.DockerError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 13 — Criar próxima release
    # ------------------------------------------------------------------

    def step_13_create_next_release(self) -> bool:
        ui.step_header(13, "Criar próxima release")
        with self._step("Criar próxima release") as step:
            branch = self.ctx.next_release
            try:
                git_ops.checkout("develop", cwd=self.repo)
                ui.success("Checkout develop")

                # Verifica se a branch já existe localmente
                local_exists = subprocess.run(
                    ["git", "branch", "--list", branch],
                    cwd=self.repo, capture_output=True, text=True,
                ).stdout.strip()

                if local_exists:
                    ui.warning(f"Branch '{branch}' já existe localmente — fazendo checkout")
                    git_ops.checkout(branch, cwd=self.repo)
                else:
                    git_ops.checkout_new(branch, cwd=self.repo)
                    ui.success(f"Branch criada: {branch}")

                # Verifica se já existe no remoto
                if git_ops.branch_exists_remote(branch, cwd=self.repo):
                    ui.warning(f"Branch '{branch}' já existe no remoto — push ignorado")
                else:
                    git_ops.push_new_branch(branch, cwd=self.repo)
                    ui.success("Push da nova release branch")

                step.ok()
                return True
            except git_ops.GitError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 14 — Atualizar package.json da próxima release
    # ------------------------------------------------------------------

    def step_14_update_package_json(self) -> bool:
        ui.step_header(14, "Atualizar package.json (próxima release)")
        with self._step("Atualizar package.json") as step:
            pkg_path = Path(self.repo) / "package.json"
            if not pkg_path.exists():
                ui.warning("package.json não encontrado — pulando")
                step.skip()
                return True

            try:
                git_ops.set_version_in_package_json(
                    str(pkg_path),
                    version=self.ctx.next_version,
                    crm_version=self.ctx.next_crm_version,
                )
                ui.success(
                    f"package.json atualizado: version={self.ctx.next_version}, "
                    f"crmVersion={self.ctx.next_crm_version}"
                )
                ui.info(f"Revise o arquivo em: {pkg_path}")

                if not self._pause("package.json ok — commitar e fazer push?"):
                    step.fail("Cancelado pelo usuário antes do commit")
                    return False

                git_ops.add(["package.json"], cwd=self.repo)
                git_ops.commit(
                    f"bump version {self.ctx.next_version}", cwd=self.repo
                )
                git_ops.push(self.ctx.next_release, cwd=self.repo)
                ui.success("Commit e push realizados")
                step.ok()
                return True
            except (git_ops.GitError, Exception) as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 15 — Gerar imagem RC
    # ------------------------------------------------------------------

    def step_15_docker_rc(self) -> bool:
        ui.step_header(15, "Gerar imagem RC")
        with self._step("Gerar imagem RC") as step:
            tag_rc = self.ctx.docker_tag_rc
            try:
                ui.info("Executando make docker-file …")
                docker_ops.make_docker_file(cwd=self.repo, timeout=1200)
                ui.success("Imagem RC construída")

                docker_ops.tag_image("zcrm_front:latest", tag_rc)
                ui.success(f"Tag RC criada: {tag_rc}")

                docker_ops.push_image(tag_rc, timeout=300)
                ui.success(f"Push RC concluído: {tag_rc}")
                step.ok()
                return True
            except docker_ops.DockerError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    # ------------------------------------------------------------------
    # Etapa 16 — Relatório final
    # ------------------------------------------------------------------

    def step_16_create_dev_branch(self) -> bool:
        ui.step_header(16, "Criar branch dev")
        with self._step("Criar branch dev") as step:
            # next_version ex: v2.55.0_zcrm_api_2.55.0 → dev/v2.55.0
            raw = self.ctx.next_version  # v2.55.0_zcrm_api_2.55.0
            semver = raw.split("_")[0]   # v2.55.0
            dev_branch = f"dev/{semver}"
            try:
                git_ops.checkout(self.ctx.next_release, cwd=self.repo)
                ui.success(f"Checkout {self.ctx.next_release}")

                local_exists = subprocess.run(
                    ["git", "branch", "--list", dev_branch],
                    cwd=self.repo, capture_output=True, text=True,
                ).stdout.strip()

                if local_exists:
                    ui.warning(f"Branch '{dev_branch}' já existe localmente — fazendo checkout")
                    git_ops.checkout(dev_branch, cwd=self.repo)
                else:
                    git_ops.checkout_new(dev_branch, cwd=self.repo)
                    ui.success(f"Branch criada: {dev_branch}")

                if git_ops.branch_exists_remote(dev_branch, cwd=self.repo):
                    ui.warning(f"Branch '{dev_branch}' já existe no remoto — push ignorado")
                else:
                    git_ops.push_new_branch(dev_branch, cwd=self.repo)
                    ui.success(f"Push da branch dev: {dev_branch}")

                self.ctx.dev_branch = dev_branch
                step.ok()
                return True
            except git_ops.GitError as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    def step_17_slack_announcement(self) -> bool:
        ui.step_header(17, "Anúncio no Slack")
        with self._step("Anúncio no Slack") as step:
            try:
                from modules import slack_ops

                raw = self.ctx.next_version          # v2.55.0_zcrm_api_2.55.0
                semver = raw.split("_")[0]           # v2.55.0
                dev_branch = getattr(self.ctx, "dev_branch", f"dev/{semver}")

                text = (
                    f":warning:  Release {self.ctx.crm_version} "
                    f"({self.ctx.frontend_version}) fechada.\n\n"
                    f"A branch {self.ctx.next_release} foi liberada. "
                    f"(Release {self.ctx.next_crm_version})\n\n\n"
                    f"• Criado a branch {dev_branch} a partir dela\n"
                    f"• Criar as branches de desenvolvimento e hotfix da sprint atual a partir dela\n\n\n"
                    f"Lembrete:\n\n"
                    f"• Não realizar merge de outras branches (incluindo a dev) nas branches de desenvolvimento e hotfix\n"
                    f"• A branch dev pode ser atualizada livremente segundo a organização de vocês"
                )

                slack_ops.post_plain_message(text)
                ui.success("Anúncio postado no Slack")
                step.ok()
                return True
            except Exception as e:
                ui.error(str(e))
                step.fail(str(e))
                return False

    def step_18_final_report(self) -> bool:
        ui.step_header(18, "Relatório final")
        with self._step("Relatório final") as step:
            from modules import report as report_mod

            path = report_mod.generate(self.ctx)
            ui.success(f"Relatório salvo em: {path}")
            step.ok()
            return True

    # ------------------------------------------------------------------
    # Checkpoint helpers
    # ------------------------------------------------------------------

    _CHECKPOINT_FILE = Path("/tmp/release_checkpoint.json")

    def _save_checkpoint(self, last_step: int, actions: dict) -> None:
        """Persist pipeline state so a failed step can be retried without restarting."""
        try:
            data = {
                "resume_from_step": last_step + 1,
                "starting_head":    self._starting_head or "",
                "current_release":  self.ctx.current_release,
                "frontend_version": self.ctx.frontend_version,
                "crm_version":      self.ctx.crm_version,
                "next_release":     self.ctx.next_release,
                "next_crm_version": self.ctx.next_crm_version,
                "branch_names":     self.ctx.branch_names,
                "branch_entries":   self.ctx.branch_entries,
                "merged_branches":  self.ctx.merged_branches,
                "failed_branches":  self.ctx.failed_branches,
                "changelog_entries": self.ctx.changelog_entries,
                "docker_registry":  self.ctx.docker_registry,
                "dev_branch":       self.ctx.dev_branch,
                "actions":          actions,
                "repo":             self.repo,
            }
            self._CHECKPOINT_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    @classmethod
    def restore_from_checkpoint(cls, cp: dict, console, repo_path: str = "."):
        """Rebuild a ReleaseContext from a saved checkpoint dict."""
        from modules.context import ReleaseContext
        ctx = ReleaseContext()
        ctx.current_release  = cp.get("current_release", "")
        ctx.frontend_version = cp.get("frontend_version", "")
        ctx.crm_version      = cp.get("crm_version", "")
        ctx.next_release     = cp.get("next_release", "")
        ctx.next_crm_version = cp.get("next_crm_version", "")
        ctx.branch_names     = cp.get("branch_names", [])
        ctx.branch_entries   = cp.get("branch_entries", [])
        ctx.merged_branches  = cp.get("merged_branches", [])
        ctx.failed_branches  = cp.get("failed_branches", [])
        ctx.changelog_entries = cp.get("changelog_entries", [])
        ctx.docker_registry  = cp.get("docker_registry", "zanthusinovacao1/zcrm_front")
        ctx.dev_branch       = cp.get("dev_branch", "")
        repo = cp.get("repo", repo_path) if repo_path == "." else repo_path
        return cls(ctx, console, repo_path=repo, step_by_step=False)

    # ------------------------------------------------------------------
    # Orchestrator
    # ------------------------------------------------------------------

    def run(self, actions: dict, resume_from_step: int = 0) -> None:
        """Execute steps based on selected actions.

        resume_from_step: when > 0, skip all steps whose number is below
        this value (uses function name convention step_NN_...).
        """
        self.console.print()

        close_steps = [
            self.step_01_validate_environment,
            self.step_02_checkout_release,
            self.step_03_collect_branches,
        ]

        close_steps_after_merge = [
            self.step_05_update_changelog,
            self.step_06_validate_package_json,
        ]

        tag_and_merge = [
            self.step_08_create_tag,
            self.step_09_update_develop,
            self.step_10_update_master,
        ]

        docker_steps = [
            self.step_11_docker_build,
            self.step_12_docker_push,
        ]

        next_release_steps = [
            self.step_13_create_next_release,
            self.step_14_update_package_json,
        ]

        rc_step = self.step_15_docker_rc
        dev_branch_step = self.step_16_create_dev_branch
        slack_step = self.step_17_slack_announcement
        report_step = self.step_18_final_report

        # Etapas que pedem confirmação antes de continuar (quando step_by_step=True)
        pause_after = {
            self.step_04_merge_branches: "Merges OK — continuar com changelog?",
            self.step_10_update_master: "Develop e master atualizados — continuar com Docker?",
            self.step_12_docker_push: "Docker publicado — continuar com próxima release?",
        }

        pipeline: list = []

        if actions.get("close"):
            pipeline += close_steps
            if actions.get("merge"):
                pipeline.append(self.step_04_merge_branches)
            pipeline += close_steps_after_merge
            pipeline += tag_and_merge

        if actions.get("image") and actions.get("publish"):
            pipeline += docker_steps

        if actions.get("next"):
            pipeline += next_release_steps

        if actions.get("rc") and actions.get("publish"):
            pipeline.append(rc_step)

        if actions.get("next"):
            pipeline.append(dev_branch_step)
            pipeline.append(slack_step)

        pipeline.append(report_step)

        # HEAD saved after step_02 (post-pull) so rollback is safe to that state
        starting_head: Optional[str] = None

        for step_fn in pipeline:
            # Extract step number from function name convention: step_NN_...
            try:
                step_num = int(step_fn.__name__.split('_')[1])
            except (ValueError, IndexError):
                step_num = 0

            # Skip steps already completed when resuming from a checkpoint
            if resume_from_step and step_num and step_num < resume_from_step:
                ui.info(f"Pulando etapa {step_num:02d} (retomada a partir da etapa {resume_from_step})")
                continue

            try:
                ok = step_fn()
            except Exception as exc:
                ui.error(f"Exceção não tratada na etapa {step_num:02d}: {exc}")
                if step_num:
                    print(f"STEP_FAIL|{step_num}", flush=True)
                if starting_head:
                    self._rollback_release(self.ctx.current_release, starting_head)
                if step_fn is not report_step:
                    self.step_18_final_report()
                break

            # Capture clean starting point right after checkout+pull
            if step_fn is self.step_02_checkout_release and ok:
                try:
                    starting_head = git_ops.rev_parse_head(cwd=self.repo)
                    self._starting_head = starting_head
                except git_ops.GitError:
                    pass

            if not ok:
                if step_num:
                    print(f"STEP_FAIL|{step_num}", flush=True)
                    # Pausa: deixa o dashboard perguntar ao usuário o que fazer
                    print(f"STEP_FAIL_PAUSE|{step_num}", flush=True)

                decision = self._wait_for_fail_decision()

                if decision == "skip":
                    ui.warning(f"Etapa {step_num:02d} pulada pelo usuário — continuando pipeline.")
                    print(f"STEP_SKIPPED|{step_num}", flush=True)
                    continue  # pula para próxima etapa sem rollback

                # abort
                ui.error("Pipeline interrompida pelo usuário.")
                if starting_head:
                    self._rollback_release(self.ctx.current_release, starting_head)
                if step_fn is not report_step:
                    self.step_18_final_report()
                break

            # Persist state after each successful step so the user can retry
            # a later failure without restarting from scratch.
            if step_num and step_fn is not report_step:
                self._save_checkpoint(step_num, actions)

            if step_fn in pause_after:
                if not self._pause(pause_after[step_fn]):
                    self.step_18_final_report()
                    break

        self.console.print()
        ui.section("Resumo das etapas")
        ui.summary_table(self.ctx.step_results)


# ------------------------------------------------------------------
# Internal step timing context manager
# ------------------------------------------------------------------


class _StepContext:
    def __init__(self, ctx: ReleaseContext, name: str, console: Console):
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
        return False  # do not suppress exceptions
