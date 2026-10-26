#!/usr/bin/env python3
"""
ZCRM Release Dashboard — servidor local
Rode:  ./dashboard.sh
Abra:  http://localhost:5050
"""

import json
import os
import queue
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import yaml

try:
    from flask import Flask, Response, jsonify, request, stream_with_context
except ImportError:
    print("Instalando Flask no venv...")
    venv_pip = Path(__file__).parent / ".venv" / "bin" / "pip"
    if venv_pip.exists():
        subprocess.check_call([str(venv_pip), "install", "--quiet", "flask"])
    else:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "flask",
                               "--break-system-packages"])
    from flask import Flask, Response, jsonify, request, stream_with_context

app = Flask(__name__)
BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "dashboard_config.json"
TASKS_FILE  = BASE_DIR / "dashboard_tasks.json"
REPORT_FILE   = BASE_DIR / "release-report.md"
RELEASE_SH    = BASE_DIR / "release.sh"
FORMALIZ_FILE = BASE_DIR / "formalizacoes.json"
HISTORY_FILE  = BASE_DIR / "release_history.json"

_process       = None
_process_lock  = threading.Lock()
_process_alive = False
_process_paused = False
_current_step  = 0
_active_pipeline = "release"   # "release" | "hotfix" — qual pipeline está rodando/rodou por último
_subscribers: list[queue.Queue] = []


# ── helpers ───────────────────────────────────────────────────────────────────

def _broadcast(msg: dict):
    dead = []
    for q in _subscribers:
        try:
            q.put_nowait(msg)
        except Exception:
            dead.append(q)
    for q in dead:
        try: _subscribers.remove(q)
        except ValueError: pass


def _default_config() -> dict:
    return {
        "current_release": "release/v2.53.0_zcrm_api_2.53.0_RC",
        "version":         "v2.53.0_zcrm_api_2.53.0",
        "crm_version":     "55.0",
        "next_release":    "release/v2.54.0_zcrm_api_2.54.0_RC",
        "next_version":    "v2.54.0_zcrm_api_2.54.0",
        "next_crm":        "56.0",
        "docker_registry": "zanthusinovacao1/zcrm_front",
        "current_sprint":  "Sprint 55",
        "team_name":       "Front-end ZCRM",
        "slack_user_id":   "",
    }


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            d = json.loads(CONFIG_FILE.read_text())
            base = _default_config()
            base.update(d)
            return base
        except Exception:
            pass
    return _default_config()


def _save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))


def _load_tasks() -> list:
    if TASKS_FILE.exists():
        try: return json.loads(TASKS_FILE.read_text())
        except Exception: pass
    return []


def _save_tasks(tasks: list):
    TASKS_FILE.write_text(json.dumps(tasks, indent=2, ensure_ascii=False))


def _load_fz_store() -> dict:
    if FORMALIZ_FILE.exists():
        try:
            raw = json.loads(FORMALIZ_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, list):           # migra formato antigo
                return {"reply_spec": "", "items": raw}
            return raw
        except Exception:
            pass
    return {"reply_spec": "", "items": []}


def _save_fz_store(store: dict) -> None:
    FORMALIZ_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_formalizacoes() -> list:
    return _load_fz_store().get("items", [])


def _save_formalizacoes(items: list) -> None:
    store = _load_fz_store()
    store["items"] = items
    _save_fz_store(store)


def _load_history() -> list:
    if HISTORY_FILE.exists():
        try: return json.loads(HISTORY_FILE.read_text())
        except Exception: pass
    return []

def _save_history_entry(entry: dict) -> None:
    entries = _load_history()
    entries.insert(0, entry)
    HISTORY_FILE.write_text(json.dumps(entries[:200], indent=2, ensure_ascii=False))


# ── rotas API ─────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return HTML

@app.route("/api/config", methods=["GET"])
def api_get_config():
    return jsonify(_load_config())

@app.route("/api/config", methods=["POST"])
def api_set_config():
    _save_config(request.json)
    return jsonify({"ok": True})

@app.route("/api/tasks", methods=["GET"])
def api_get_tasks():
    return jsonify(_load_tasks())

@app.route("/api/tasks", methods=["POST"])
def api_set_tasks():
    _save_tasks(request.json)
    return jsonify({"ok": True})

@app.route("/api/release/status")
def api_status():
    return jsonify({"running": _process_alive, "paused": _process_paused, "step": _current_step, "pipeline": _active_pipeline})


@app.route("/api/release/pause", methods=["POST"])
@app.route("/api/hotfix/pause", methods=["POST"])
def api_pause():
    import signal as _signal
    global _process_paused
    if not _process_alive or not _process:
        return jsonify({"ok": False, "error": "Nenhum processo em execução"}), 400
    try:
        os.kill(_process.pid, _signal.SIGSTOP)
        _process_paused = True
        _broadcast({"t": "paused"})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/release/resume", methods=["POST"])
@app.route("/api/hotfix/resume", methods=["POST"])
def api_resume():
    import signal as _signal
    global _process_paused
    if not _process_alive or not _process:
        return jsonify({"ok": False, "error": "Nenhum processo em execução"}), 400
    try:
        os.kill(_process.pid, _signal.SIGCONT)
        _process_paused = False
        _broadcast({"t": "resumed"})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def _stream_process(cmd: list[str], cwd: Path, pipeline: str, resume_from_step: int = 0):
    """
    Executa 'cmd' e repassa cada linha de stdout via SSE (_broadcast), reconhecendo
    o protocolo emitido por modules/ui.py tanto do pipeline de release quanto do
    de hotfix (STEP_FAIL|N, CONFLICT_PAUSE|branches, cabeçalho "▶ Etapa NN").

    Compartilhado entre /api/release/run, /api/release/retry-step, /api/hotfix/run
    e /api/hotfix/retry-step para não duplicar esse parsing quatro vezes.
    """
    global _process, _process_alive, _process_paused, _current_step, _active_pipeline
    import re as _re

    _process_alive  = True
    _current_step   = 0
    _active_pipeline = pipeline
    _broadcast({"t": "start", "from_step": resume_from_step})

    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["FORCE_COLOR"]      = "0"   # disable rich colors → cleaner output

        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, bufsize=1, env=env,
        )
        _process = proc

        _ansi = _re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-9;]*[mGKHJF]|\[\?[0-9;]*[lh])')
        current_step          = 0
        current_step_failed   = False  # True when STEP_FAIL|N was received
        error_buf             = []

        import time as _time
        _run_start  = _time.time()
        _steps_ok   = 0
        _steps_fail = 0
        _steps_skip = 0

        for raw_line in proc.stdout:
            line  = raw_line.rstrip("\n")
            clean = _ansi.sub("", line).strip()

            # STEP_FAIL protocol: steps.py prints this when a step fails,
            # so the dashboard knows the NEXT step-header transition is a
            # failure, not a success.
            if clean.startswith("STEP_FAIL|"):
                try:
                    fail_num = int(clean.split("|")[1])
                    current_step_failed = True
                    _steps_fail += 1
                    err_detail = " · ".join(error_buf[-5:]) if error_buf else "Verifique o log da etapa"
                    _broadcast({"t": "step_done", "step": fail_num,
                                "status": "failed", "error": err_detail})
                except (ValueError, IndexError):
                    pass

            # Step failure pause — aguarda decisão do usuário no dashboard
            if clean.startswith("STEP_FAIL_PAUSE|"):
                try:
                    fail_num = int(clean.split("|")[1])
                    err_detail = " · ".join(error_buf[-5:]) if error_buf else "Verifique o log da etapa"
                    _broadcast({"t": "step_fail_pause", "step": fail_num, "error": err_detail})
                    _dm_uid = _load_config().get("slack_user_id") or os.getenv("SLACK_USER_DM", "")
                    if _dm_uid:
                        try:
                            from modules.slack_ops import send_dm as _sdm
                            _pl_lbl = "Release" if pipeline == "release" else "Hotfix"
                            _sdm(_dm_uid,
                                f"⚠️ *Pipeline {_pl_lbl} parou — sua ação é necessária*\n"
                                f"Etapa: *{fail_num}* falhou\n"
                                f"Erro: {err_detail}\n"
                                f"Acesse o dashboard para pular a etapa ou abortar.")
                        except Exception:
                            pass
                except (ValueError, IndexError):
                    pass

            if clean == "WAITING_FAIL_DECISION":
                pass  # sinalizado via STEP_FAIL_PAUSE acima

            if clean.startswith("STEP_SKIPPED|"):
                try:
                    skip_num = int(clean.split("|")[1])
                    _steps_skip += 1
                    _broadcast({"t": "step_done", "step": skip_num, "status": "skipped"})
                except (ValueError, IndexError):
                    pass

            # Conflict pause signal from steps.py / hotfix_steps.py
            if clean.startswith("CONFLICT_PAUSE|"):
                branches = [b for b in clean.split("|")[1:] if b]
                _broadcast({"t": "conflict_pause", "branches": branches, "step": current_step})
                _dm_uid = _load_config().get("slack_user_id") or os.getenv("SLACK_USER_DM", "")
                if _dm_uid:
                    try:
                        from modules.slack_ops import send_dm as _sdm
                        _pl_lbl = "Release" if pipeline == "release" else "Hotfix"
                        _br_list = ", ".join(branches) if branches else "—"
                        _sdm(_dm_uid,
                            f"🔀 *Conflito de merge detectado — {_pl_lbl}*\n"
                            f"Etapa: *{current_step}*\n"
                            f"Branches: {_br_list}\n"
                            f"Acesse o dashboard para resolver o conflito e continuar.")
                    except Exception:
                        pass

            # Detect step header: "▶  Etapa NN  Step name"
            m = _re.search(r'▶\s+Etapa\s+(\d+)\s+(.+)', clean)
            if m:
                # Only send step_done for the previous step if it wasn't
                # already sent via STEP_FAIL (to avoid double-marking).
                if current_step > 0 and not current_step_failed:
                    _steps_ok += 1
                    _broadcast({"t": "step_done", "step": current_step, "status": "success"})
                current_step        = int(m.group(1))
                current_step_failed = False
                _current_step       = current_step
                error_buf           = []
                _broadcast({"t": "step_start", "step": current_step, "name": m.group(2).strip()})

            # Collect error context for descriptive failure messages
            cl = clean.lower()
            if any(k in cl for k in ("conflict", "✗", "falhou", "fatal", "error:", "erro:", "connection timed out", "timeout")):
                error_buf.append(clean)
            if "automatic merge failed" in cl:
                error_buf.append(clean)

            _broadcast({"t": "line", "v": line, "step": current_step})

        proc.wait()
        _current_step = 0

        # Final step_done only if the step wasn't already marked via STEP_FAIL
        if current_step > 0 and not current_step_failed:
            if proc.returncode != 0:
                _steps_fail += 1
                err_detail = " · ".join(error_buf[-5:]) if error_buf else "Verifique o log da etapa"
                _broadcast({"t": "step_done", "step": current_step,
                            "status": "failed", "error": err_detail})
            else:
                _steps_ok += 1
                _broadcast({"t": "step_done", "step": current_step, "status": "success"})

        _broadcast({"t": "done", "code": proc.returncode})

        # Record this run in pipeline history
        import datetime as _dt, uuid as _uuid
        _run_end  = _time.time()
        _duration = int(_run_end - _run_start)
        _outcome  = "success" if proc.returncode == 0 else "failed"
        _cfg = _load_config()
        _hist_entry = {
            "id":           _uuid.uuid4().hex[:8],
            "pipeline":     pipeline,
            "started_at":   _dt.datetime.fromtimestamp(_run_start).isoformat(timespec="seconds"),
            "finished_at":  _dt.datetime.fromtimestamp(_run_end).isoformat(timespec="seconds"),
            "duration_s":   _duration,
            "outcome":      _outcome,
            "steps_ok":     _steps_ok,
            "steps_fail":   _steps_fail,
            "steps_skip":   _steps_skip,
            "steps_total":  _steps_ok + _steps_fail + _steps_skip,
            "version":      _cfg.get("next_version") or _cfg.get("version", ""),
            "release_branch": _cfg.get("next_release") or _cfg.get("current_release", ""),
            "from_step":    resume_from_step,
        }
        try:
            _save_history_entry(_hist_entry)
        except Exception:
            pass

        # Build end-of-pipeline message for Slack notifications
        _pl_lbl   = "Release" if pipeline == "release" else "Hotfix"
        _emoji    = "✅" if _outcome == "success" else "❌"
        _dur_m    = _duration // 60
        _dur_s    = _duration % 60
        _dur_str  = f"{_dur_m}m {_dur_s}s" if _dur_m else f"{_dur_s}s"
        _end_msg  = (
            f"{_emoji} *Pipeline {_pl_lbl} {'concluída com sucesso' if _outcome=='success' else 'falhou'}*\n"
            f"Versão: `{_hist_entry['version'] or '—'}`\n"
            f"Duração: {_dur_str} · Etapas: {_steps_ok}✓ {_steps_fail}✗ {_steps_skip}⊘"
        )

        # Channel notification (optional — set SLACK_PIPELINE_CHANNEL in .env)
        _notif_channel = os.getenv("SLACK_PIPELINE_CHANNEL", "")
        if _notif_channel:
            try:
                from modules.slack_ops import post_plain_message as _slk_post
                _slk_post(_end_msg, channel=_notif_channel)
            except Exception:
                pass

        # DM notification (optional — configure slack_user_id in settings or SLACK_USER_DM in .env)
        _dm_uid = _load_config().get("slack_user_id") or os.getenv("SLACK_USER_DM", "")
        if _dm_uid:
            try:
                from modules.slack_ops import send_dm as _sdm
                _sdm(_dm_uid, _end_msg)
            except Exception:
                pass

    except Exception as exc:
        _broadcast({"t": "error", "v": str(exc)})
    finally:
        _process_alive  = False
        _process_paused = False
        _current_step   = 0
        _process = None
        _broadcast({"t": "end"})


@app.route("/api/release/run", methods=["POST"])
def api_run():
    global _process_alive
    with _process_lock:
        if _process_alive:
            return jsonify({"ok": False, "error": "Já está rodando"}), 400

        data = request.json or {}
        from_step = int(data.get("from_step", 1))

        # When starting from a specific step, write a synthetic checkpoint so
        # release.py --resume-from-step can load the context from the wizard fields.
        if from_step > 1:
            # Recover persisted branch/merge data from last checkpoint so
            # steps 03-04 don't have to re-query Slack when jumping ahead.
            existing_cp: dict = {}
            try:
                existing_cp = json.loads(
                    Path("/tmp/release_checkpoint.json").read_text(encoding="utf-8")
                )
            except Exception:
                pass

            cp_data = {
                "resume_from_step": from_step,
                "current_release":  data.get("current_release", ""),
                "frontend_version": data.get("frontend_version", ""),
                "crm_version":      data.get("crm_version", ""),
                "next_release":     data.get("next_release", ""),
                "next_crm_version": data.get("next_crm_version", ""),
                # Use wizard-supplied branches if given; otherwise reuse
                # from last checkpoint so Slack is not queried again.
                "branch_names":     data.get("branches") or existing_cp.get("branch_names", []),
                "branch_entries":   existing_cp.get("branch_entries", []),
                "merged_branches":  existing_cp.get("merged_branches", []) if from_step > 4 else [],
                "failed_branches":  existing_cp.get("failed_branches", []) if from_step > 4 else [],
                "changelog_entries": existing_cp.get("changelog_entries", []) if from_step > 5 else [],
                "docker_registry":  _load_config().get("docker_registry", ""),
                "actions": {"close": True, "merge": True, "image": True,
                            "publish": True, "next": True, "rc": True},
                "repo": ".",
            }
            try:
                Path("/tmp/release_checkpoint.json").write_text(
                    json.dumps(cp_data, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            except Exception as exc:
                return jsonify({"ok": False, "error": f"Falha ao criar checkpoint: {exc}"}), 500

        venv_py    = BASE_DIR / ".venv" / "bin" / "python3"
        release_py = BASE_DIR / "release.py"

        if from_step > 1:
            cmd = [str(venv_py), str(release_py), "--yes",
                   "--resume-from-step", str(from_step)]
        else:
            cmd = [str(venv_py), str(release_py), "--yes"]

            if data.get("current_release"):
                cmd += ["--current-release", data["current_release"]]
            if data.get("frontend_version"):
                cmd += ["--frontend-version", data["frontend_version"]]
            if data.get("crm_version"):
                cmd += ["--crm-version", data["crm_version"]]
            if data.get("next_release"):
                cmd += ["--next-release", data["next_release"]]
            if data.get("next_crm_version"):
                cmd += ["--next-crm-version", data["next_crm_version"]]
            branches = data.get("branches") or []
            if branches:
                cmd += ["--branches"] + branches

        threading.Thread(target=_stream_process, args=(cmd, BASE_DIR, "release"), daemon=True).start()
        return jsonify({"ok": True})

@app.route("/api/release/stop", methods=["POST"])
def api_stop():
    if _process:
        _process.terminate()
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Nenhum processo em execução"})


@app.route("/api/release/conflict-resume", methods=["POST"])
@app.route("/api/hotfix/conflict-resume", methods=["POST"])
def api_conflict_resume():
    # Toca os marcadores dos dois pipelines — apenas um estará de fato em uso
    # (só um processo roda por vez), então tocar o outro é inofensivo.
    Path("/tmp/release_conflict_resume").touch()
    Path("/tmp/hotfix_conflict_resume").touch()
    return jsonify({"ok": True})

@app.route("/api/release/fail-decision", methods=["POST"])
@app.route("/api/hotfix/fail-decision", methods=["POST"])
def api_fail_decision():
    decision = (request.json or {}).get("decision", "abort")
    if decision not in ("skip", "abort"):
        return jsonify({"ok": False, "error": "decisão inválida"}), 400
    Path("/tmp/release_fail_decision").write_text(decision)
    return jsonify({"ok": True})


@app.route("/api/release/rollback-info")
def api_rollback_info():
    """Retorna informações para o rollback: starting_head e commits feitos pela pipeline."""
    import json as _json
    cp_file = Path("/tmp/release_checkpoint.json")
    if not cp_file.exists():
        return jsonify({"ok": False, "error": "Nenhum checkpoint encontrado — nada para desfazer."})

    try:
        cp = _json.loads(cp_file.read_text())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

    repo = cp.get("repo", "")
    starting_head = cp.get("starting_head", "")
    if not repo or not Path(repo).exists():
        return jsonify({"ok": False, "error": f"Repositório não encontrado: {repo}"})

    import re as _re
    release_branch = cp.get("current_release", "")

    try:
        if starting_head:
            # SHA salvo — listamos exatamente os commits da pipeline
            r = subprocess.run(
                ["git", "log", "--oneline", f"{starting_head}..HEAD"],
                cwd=repo, capture_output=True, text=True, timeout=10
            )
            commits = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
            detected_head = starting_head
        else:
            # SHA não salvo (checkpoint antigo) — detecta automaticamente
            # Padrões: merge "... into release/..." ou "changelog vX.Y..."
            _merge_pat = _re.compile(r"Merge (branch|remote-tracking branch) .+ into " + _re.escape(release_branch), _re.IGNORECASE)
            _chlog_pat = _re.compile(r"^changelog\s+v", _re.IGNORECASE)

            r = subprocess.run(
                ["git", "log", "--oneline", "--first-parent", "-60"],
                cwd=repo, capture_output=True, text=True, timeout=10
            )
            all_lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]

            pipeline_commits = []
            detected_head = ""
            for line in all_lines:
                sha, _, msg = line.partition(" ")
                if _merge_pat.search(msg) or _chlog_pat.match(msg):
                    pipeline_commits.append(line)
                else:
                    detected_head = sha
                    break

            commits = pipeline_commits
            starting_head = detected_head  # usa como SHA de destino

        return jsonify({
            "ok": True,
            "repo": repo,
            "starting_head": starting_head,
            "current_release": release_branch,
            "commits": commits,
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@app.route("/api/release/rollback", methods=["POST"])
def api_rollback():
    """Faz git reset --hard ao ponto anterior à pipeline."""
    import json as _json
    if _process_alive:
        return jsonify({"ok": False, "error": "Pipeline em execução — pare antes de reverter."}), 400

    cp_file = Path("/tmp/release_checkpoint.json")
    if not cp_file.exists():
        return jsonify({"ok": False, "error": "Nenhum checkpoint encontrado."}), 400

    cp = _json.loads(cp_file.read_text())
    repo  = cp.get("repo", "")
    target = (request.json or {}).get("target_sha") or cp.get("starting_head", "")

    if not target:
        return jsonify({"ok": False, "error": "SHA de destino não encontrado. Passe target_sha no corpo."}), 400
    if not repo or not Path(repo).exists():
        return jsonify({"ok": False, "error": f"Repositório não encontrado: {repo}"}), 400

    try:
        r = subprocess.run(
            ["git", "reset", "--hard", target],
            cwd=repo, capture_output=True, text=True, timeout=15
        )
        if r.returncode != 0:
            return jsonify({"ok": False, "error": r.stderr.strip() or r.stdout.strip()}), 500

        cp_file.unlink(missing_ok=True)
        return jsonify({"ok": True, "message": f"Reset aplicado: HEAD agora em {target[:8]}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/release/retry-step", methods=["POST"])
def api_retry_step():
    with _process_lock:
        if _process_alive:
            return jsonify({"ok": False, "error": "Pipeline já está rodando"}), 400

        data = request.json or {}
        step = int(data.get("step", 0))
        if not step:
            return jsonify({"ok": False, "error": "step inválido"}), 400

        cp_file = Path("/tmp/release_checkpoint.json")
        if not cp_file.exists():
            return jsonify({"ok": False, "error": "Checkpoint não encontrado. Reinicie a pipeline completa."}), 400

        venv_py    = BASE_DIR / ".venv" / "bin" / "python3"
        release_py = BASE_DIR / "release.py"
        cmd = [str(venv_py), str(release_py), "--yes", "--resume-from-step", str(step)]

        threading.Thread(target=_stream_process, args=(cmd, BASE_DIR, "release", step), daemon=True).start()
        return jsonify({"ok": True, "from_step": step})


@app.route("/api/hotfix/run", methods=["POST"])
def api_hotfix_run():
    with _process_lock:
        if _process_alive:
            return jsonify({"ok": False, "error": "Já está rodando"}), 400

        data = request.json or {}
        venv_py   = BASE_DIR / ".venv" / "bin" / "python3"
        hotfix_py = BASE_DIR / "hotfix.py"

        cmd = [str(venv_py), str(hotfix_py), "--yes"]
        if data.get("hotfix_branch"):
            cmd += ["--hotfix-branch", data["hotfix_branch"]]
        if data.get("target_release"):
            cmd += ["--target-release", data["target_release"]]
        if data.get("hotfix_version"):
            cmd += ["--hotfix-version", data["hotfix_version"]]
        if data.get("ticket"):
            cmd += ["--ticket", data["ticket"]]
        if data.get("description"):
            cmd += ["--description", data["description"]]
        if data.get("is_previous_release"):
            cmd += ["--previous"]
            if data.get("current_release"):
                cmd += ["--current-release", data["current_release"]]
        if data.get("docker_registry"):
            cmd += ["--docker-registry", data["docker_registry"]]
        from_step = int(data.get("from_step", 1))
        if from_step > 1:
            cmd += ["--resume-from-step", str(from_step)]

        threading.Thread(target=_stream_process, args=(cmd, BASE_DIR, "hotfix", from_step), daemon=True).start()
        return jsonify({"ok": True})

@app.route("/api/hotfix/retry-step", methods=["POST"])
def api_hotfix_retry_step():
    with _process_lock:
        if _process_alive:
            return jsonify({"ok": False, "error": "Pipeline já está rodando"}), 400

        data = request.json or {}
        step = int(data.get("step", 0))
        if not step:
            return jsonify({"ok": False, "error": "step inválido"}), 400

        cp_file = Path("/tmp/hotfix_checkpoint.json")
        if not cp_file.exists():
            return jsonify({"ok": False, "error": "Checkpoint não encontrado. Reinicie o hotfix completo."}), 400

        venv_py   = BASE_DIR / ".venv" / "bin" / "python3"
        hotfix_py = BASE_DIR / "hotfix.py"
        cmd = [str(venv_py), str(hotfix_py), "--yes", "--resume-from-step", str(step)]

        threading.Thread(target=_stream_process, args=(cmd, BASE_DIR, "hotfix"), daemon=True).start()
        return jsonify({"ok": True})

@app.route("/api/release/stream")
def api_stream():
    q: queue.Queue = queue.Queue()
    _subscribers.append(q)

    def gen():
        try:
            while True:
                try:
                    msg = q.get(timeout=25)
                    yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
                    if msg.get("t") == "end":
                        break
                except queue.Empty:
                    yield 'data: {"t":"ping"}\n\n'
        finally:
            try: _subscribers.remove(q)
            except ValueError: pass

    return Response(
        stream_with_context(gen()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.route("/api/report")
def api_report():
    if REPORT_FILE.exists():
        return REPORT_FILE.read_text(encoding="utf-8"), 200, {"Content-Type": "text/plain"}
    return "", 404

@app.route("/api/hotfix-report")
def api_hotfix_report():
    hotfix_report = BASE_DIR / "hotfix-report.md"
    if hotfix_report.exists():
        return hotfix_report.read_text(encoding="utf-8"), 200, {"Content-Type": "text/plain"}
    return "", 404

@app.route("/api/history")
def api_history():
    return jsonify(_load_history())

@app.route("/api/notify/dm", methods=["POST"])
def api_notify_dm():
    data = request.json or {}
    text    = data.get("text", "").strip()
    user_id = data.get("user_id") or _load_config().get("slack_user_id") or os.getenv("SLACK_USER_DM", "")
    if not user_id:
        return jsonify({"ok": False, "error": "slack_user_id não configurado"}), 400
    if not text:
        return jsonify({"ok": False, "error": "text vazio"}), 400
    try:
        from modules.slack_ops import send_dm
        ts = send_dm(user_id, text)
        return jsonify({"ok": True, "ts": ts})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/git/branches")
def api_git_branches():
    ticket = request.args.get("ticket", "").strip().upper()
    if not ticket:
        return jsonify({"ok": False, "error": "ticket param required"}), 400
    repo = _load_config().get("repo_path") or str(BASE_DIR.parent / "zcrm_front")
    try:
        r1 = subprocess.run(["git", "branch", "-r"], cwd=repo, capture_output=True, text=True, timeout=10)
        r2 = subprocess.run(["git", "branch"],        cwd=repo, capture_output=True, text=True, timeout=10)
        seen, branches = set(), []
        for line in (r1.stdout + "\n" + r2.stdout).splitlines():
            b = line.strip().lstrip("*").strip()
            b = b.split("->")[0].strip()          # remove "origin/HEAD -> origin/main"
            b = b.removeprefix("origin/")
            if ticket in b.upper() and b not in seen:
                seen.add(b)
                branches.append(b)
        return jsonify({"ok": True, "branches": sorted(branches)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/formalizacoes", methods=["GET"])
def api_formalizacoes_list():
    store = _load_fz_store()
    return jsonify({"items": store.get("items", []), "reply_spec": store.get("reply_spec", "")})


@app.route("/api/formalizacoes", methods=["POST"])
def api_formalizacoes_add():
    import uuid as _uuid, datetime as _dt
    data = request.json or {}
    items = _load_formalizacoes()
    item = {
        "id":         _uuid.uuid4().hex[:8],
        "task":       data.get("task", "").strip().upper(),
        "subtask":    data.get("subtask", "").strip(),
        "branch":     data.get("branch", "").strip(),
        "conteudo":   data.get("conteudo", "").strip(),
        "status":     "pending",
        "slack_ts":   "",
        "error":      "",
        "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
    }
    items.append(item)
    _save_formalizacoes(items)
    return jsonify({"ok": True, "item": item})


@app.route("/api/formalizacoes/<item_id>", methods=["PUT"])
def api_formalizacoes_update(item_id):
    data = request.json or {}
    items = _load_formalizacoes()
    for item in items:
        if item["id"] == item_id:
            for k in ("task", "subtask", "branch", "conteudo"):
                if k in data:
                    item[k] = data[k]
            item["status"] = "pending"
            item["error"] = ""
            _save_formalizacoes(items)
            return jsonify({"ok": True, "item": item})
    return jsonify({"ok": False, "error": "not found"}), 404


@app.route("/api/formalizacoes/<item_id>", methods=["DELETE"])
def api_formalizacoes_delete(item_id):
    items = [i for i in _load_formalizacoes() if i["id"] != item_id]
    _save_formalizacoes(items)
    return jsonify({"ok": True})


@app.route("/api/formalizacoes/reply-spec", methods=["GET"])
def api_fz_reply_spec_get():
    store = _load_fz_store()
    spec = store.get("reply_spec", "")
    if not spec:
        cfg = _load_config()
        crm_raw = str(cfg.get("crm_version", ""))
        crm = crm_raw if "." in crm_raw else crm_raw + ".0"
        fe = cfg.get("version", "")
        spec = f"CRM:\nRelease {crm}\nFrontend:\n{fe}"
    return jsonify({"ok": True, "reply_spec": spec})


@app.route("/api/formalizacoes/reply-spec", methods=["PUT"])
def api_fz_reply_spec_put():
    data = request.json or {}
    store = _load_fz_store()
    store["reply_spec"] = data.get("reply_spec", "")
    _save_fz_store(store)
    return jsonify({"ok": True})


def _best_branch(branches: list) -> str:
    """
    Dentre várias branches que batem no mesmo ticket, escolhe a de maior versão.
    Reconhece sufixos como -v2.55, -v2.54.1, -v2.53 no final do nome.
    Se não houver versão no nome, mantém a ordem original (primeira).
    """
    import re as _re
    _VER_RE = _re.compile(r"-v(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[^/]*)?$", _re.IGNORECASE)

    def _ver_key(b: str):
        m = _VER_RE.search(b)
        if not m:
            return (-1, -1, -1)
        return tuple(int(x or 0) for x in m.groups())

    if not branches:
        return ""
    return max(branches, key=_ver_key)


@app.route("/api/formalizacoes/bulk", methods=["POST"])
def api_formalizacoes_bulk():
    import uuid as _uuid, datetime as _dt
    data = request.json or {}
    tickets = [t.strip().upper() for t in data.get("tickets", []) if t.strip()]
    if not tickets:
        return jsonify({"ok": False, "error": "nenhum ticket enviado"}), 400
    repo = _load_config().get("repo_path") or str(BASE_DIR.parent / "zcrm_front")
    try:
        r1 = subprocess.run(["git", "branch", "-r"], cwd=repo, capture_output=True, text=True, timeout=10)
        r2 = subprocess.run(["git", "branch"],        cwd=repo, capture_output=True, text=True, timeout=10)
        all_branches = []
        seen_b = set()
        for line in (r1.stdout + "\n" + r2.stdout).splitlines():
            b = line.strip().lstrip("*").strip().split("->")[0].strip().removeprefix("origin/")
            if b and b not in seen_b:
                seen_b.add(b)
                all_branches.append(b)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    from modules import jira_ops as _jira_ops
    _jira_configured = _jira_ops.is_configured()

    items = _load_formalizacoes()
    added = []
    for ticket in tickets:
        matches = [b for b in all_branches if ticket in b.upper()]
        branch = _best_branch(matches)

        jira_info = _jira_ops.fetch_issue_info(ticket) if _jira_configured else {}
        title   = jira_info.get("title", "")
        conteudo = f"#{ticket}# - {title}" if title else f"#{ticket}# - "
        subtask  = jira_info.get("subtask", "")

        item = {
            "id":         _uuid.uuid4().hex[:8],
            "task":       ticket,
            "subtask":    subtask,
            "branch":     branch,
            "conteudo":   conteudo,
            "status":     "pending",
            "slack_ts":   "",
            "error":      "" if branch else "Branch não encontrada — edite antes de enviar",
            "created_at": _dt.datetime.now().isoformat(timespec="seconds"),
        }
        items.append(item)
        added.append(item)
    _save_formalizacoes(items)
    return jsonify({"ok": True, "added": added})


@app.route("/api/slack/check-formalized")
def api_slack_check_formalized():
    branch = request.args.get("branch", "").strip()
    if not branch:
        return jsonify({"ok": False, "error": "branch param required"}), 400
    try:
        from modules.slack_ops import check_already_formalized
        result = check_already_formalized(branch)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/formalizacoes/<item_id>/enviar", methods=["POST"])
def api_formalizacoes_enviar(item_id):
    items = _load_formalizacoes()
    item = next((i for i in items if i["id"] == item_id), None)
    if not item:
        return jsonify({"ok": False, "error": "not found"}), 404

    # Bloqueia re-envio se já foi formalizado no Slack (a menos que force=true)
    force = (request.json or {}).get("force", False)
    if not force and item.get("branch"):
        try:
            from modules.slack_ops import check_already_formalized
            chk = check_already_formalized(item["branch"])
            if chk["formalized"]:
                return jsonify({
                    "ok": False,
                    "duplicate": True,
                    "posted_at": chk["posted_at"],
                    "error": f"Branch já formalizada no Slack em {chk['posted_at']}",
                }), 409
        except Exception:
            pass  # se não tem Slack token, deixa tentar enviar e falhar normalmente

    # Use reply_spec from store; fallback to config-derived spec
    store = _load_fz_store()
    reply_spec = store.get("reply_spec", "").strip()
    if not reply_spec:
        cfg = _load_config()
        crm_raw = str(cfg.get("crm_version", ""))
        crm = crm_raw if "." in crm_raw else crm_raw + ".0"
        fe = cfg.get("version", "")
        reply_spec = f"CRM:\nRelease {crm}\nFrontend:\n{fe}"

    try:
        from modules.slack_ops import post_formalization, reply_text
        ts = post_formalization(item["branch"], item["conteudo"], item["task"], item.get("subtask", ""))
        reply_ts = reply_text(ts, reply_spec)
        item["status"] = "sent"
        item["slack_ts"] = ts
        item["reply_ts"] = reply_ts or ""
        item["error"] = ""
    except Exception as e:
        item["status"] = "error"
        item["error"] = str(e)
        _save_formalizacoes(items)
        # DM on formalization error
        _dm_uid = _load_config().get("slack_user_id") or os.getenv("SLACK_USER_DM", "")
        if _dm_uid:
            try:
                from modules.slack_ops import send_dm
                send_dm(_dm_uid, f"❌ *Erro ao formalizar*\nTicket: {item['task']}\nBranch: `{item['branch']}`\nErro: {str(e)}")
            except Exception:
                pass
        return jsonify({"ok": False, "error": str(e)}), 500
    _save_formalizacoes(items)
    # DM on successful formalization
    _dm_uid = _load_config().get("slack_user_id") or os.getenv("SLACK_USER_DM", "")
    if _dm_uid:
        try:
            from modules.slack_ops import send_dm
            send_dm(_dm_uid, f"✅ *Formalização enviada*\nTicket: {item['task']}\nBranch: `{item['branch']}`")
        except Exception:
            pass
    return jsonify({"ok": True, "item": item})


@app.route("/api/formalizacoes/<item_id>/deletar-slack", methods=["POST"])
def api_formalizacoes_deletar_slack(item_id):
    items = _load_formalizacoes()
    item = next((i for i in items if i["id"] == item_id), None)
    if not item:
        return jsonify({"ok": False, "error": "not found"}), 404
    ts = item.get("slack_ts", "")
    if not ts:
        return jsonify({"ok": False, "error": "Mensagem não tem ts do Slack"}), 400
    try:
        from modules.slack_ops import delete_message
        # Apaga o reply da thread primeiro (se existir)
        reply_ts = item.get("reply_ts", "")
        if reply_ts:
            try:
                delete_message(reply_ts)
            except Exception:
                pass  # se já foi apagado, ignora
        # Apaga a mensagem principal
        delete_message(ts)
        item["status"] = "pending"
        item["slack_ts"] = ""
        item["reply_ts"] = ""
        item["error"] = ""
        _save_formalizacoes(items)
        return jsonify({"ok": True, "item": item})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/jira/issue-info/<issue_key>")
def api_jira_issue_info(issue_key):
    from modules import jira_ops
    if not jira_ops.is_configured():
        return jsonify({"ok": False, "title": "", "subtask": ""})
    try:
        info = jira_ops.fetch_issue_info(issue_key)
        return jsonify({"ok": True, **info})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "title": "", "subtask": ""})


@app.route("/api/jira/test")
def api_jira_test():
    from modules import jira_ops
    try:
        result = jira_ops.test_connection()
        return jsonify(result)
    except jira_ops.JiraError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

def _load_yaml_config() -> dict:
    """Lê o config.yaml do projeto (compartilhado com release.py/hotfix.py)."""
    yaml_path = BASE_DIR / "config.yaml"
    if yaml_path.exists():
        try:
            return yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except Exception:
            pass
    return {}

def _default_jira_jql() -> str:
    """
    Monta a JQL padrão a partir do template 'jira.jql_template' em config.yaml,
    substituindo {version} pelo crm_version da release ATUAL salva em
    dashboard_config.json (ex: "56" -> fixVersion = "Neosense 56").
    """
    template = _load_yaml_config().get("jira", {}).get(
        "jql_template",
        '"Categorias" = "neos-frontend" AND fixVersion = "Neosense {version}" ORDER BY updated DESC',
    )
    crm_version = str(_load_config().get("crm_version", "")).strip()
    if crm_version.endswith(".0"):
        crm_version = crm_version[:-2]
    return template.format(version=crm_version)

@app.route("/api/jira/default-jql")
def api_jira_default_jql():
    return jsonify({"jql": _default_jira_jql()})

# Rótulos legíveis para exibir as mudanças de status/tipo no modal de conflito
# (mesmos ids usados pelas colunas do kanban e pelos tipos de task no front-end).
_STATUS_LABELS = {
    "importantes": "Importantes", "planejadas": "Tarefas Planejadas",
    "solicitacoes": "Solicitações", "andamento": "Em Andamento",
    "aguardando": "Aguardando Autorização", "validacao": "Em Validação",
    "formalizar": "Para Formalizar", "formalizadas": "Formalizadas",
    "etapas": "Etapas Release", "concluidas": "Concluídas",
}
_TYPE_LABELS = {"feat": "Feature", "fix": "Fix", "chore": "Chore", "other": "Outro"}

def _display_value(field: str, value: str) -> str:
    if field == "status":
        return _STATUS_LABELS.get(value, value) or "—"
    if field == "type":
        return _TYPE_LABELS.get(value, value) or "—"
    return value or "—"

@app.route("/api/jira/preview", methods=["POST"])
def api_jira_preview():
    """
    Busca as issues no Jira e compara com o que já existe localmente, SEM
    salvar nada ainda. Tarefas novas (sem correspondência local) entram direto
    na lista 'new_tasks'. Tarefas que já existem e mudaram de algum campo
    desde a última importação entram em 'conflicts', para o usuário decidir
    individualmente (no dashboard) se quer sincronizar ou manter o que
    moveu/editou manualmente.
    """
    from modules import jira_ops

    data = request.json or {}
    jql = data.get("jql") or _default_jira_jql()

    try:
        fetched = jira_ops.fetch_issues(jql)
    except jira_ops.JiraError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    existing = _load_tasks()
    by_jira = {t.get("jira"): t for t in existing if t.get("jira")}

    compare_fields = (
        ("title", "Título"), ("type", "Tipo"), ("status", "Status"),
        ("person", "Responsável"), ("sprint", "Sprint"),
    )

    new_tasks: list = []
    conflicts: list = []
    unchanged = 0

    for incoming in fetched:
        key = incoming["jira"]
        old = by_jira.get(key)
        if not old:
            new_tasks.append(incoming)
            continue

        changes = []
        for field, label in compare_fields:
            new_val = incoming.get(field, "")
            old_val = old.get(field, "")
            if not new_val or old_val == new_val:
                continue
            changes.append({
                "field": field,
                "label": label,
                "old": _display_value(field, old_val),
                "new": _display_value(field, new_val),
            })

        if changes:
            conflicts.append({
                "jira": key,
                "title": old.get("title") or incoming["title"],
                "changes": changes,
                "incoming": incoming,
            })
        else:
            unchanged += 1

    return jsonify({
        "ok": True,
        "new_tasks": new_tasks,
        "conflicts": conflicts,
        "unchanged_count": unchanged,
        "total_fetched": len(fetched),
    })

@app.route("/api/jira/apply", methods=["POST"])
def api_jira_apply():
    """
    Aplica o resultado de /api/jira/preview: adiciona as tarefas novas
    (sempre) e atualiza apenas as tarefas de 'accepted' (as que o usuário
    confirmou que quer sincronizar com o Jira) — as demais ficam como estão
    localmente (ex: card movido manualmente para outra coluna).
    """
    data = request.json or {}
    new_tasks = data.get("new_tasks") or []
    accepted  = data.get("accepted") or []

    existing = _load_tasks()
    by_jira = {t.get("jira"): t for t in existing if t.get("jira")}

    created = 0
    for t in new_tasks:
        if t.get("jira") in by_jira:
            continue  # já existe localmente (corrida rara) — ignora
        existing.append(t)
        by_jira[t.get("jira")] = t
        created += 1

    updated = 0
    for incoming in accepted:
        key = incoming.get("jira")
        old = by_jira.get(key)
        if not old:
            continue
        old["title"]  = incoming.get("title", old.get("title"))
        old["type"]   = incoming.get("type", old.get("type"))
        old["status"] = incoming.get("status", old.get("status"))
        old["person"] = incoming.get("person") or old.get("person", "")
        old["sprint"] = incoming.get("sprint") or old.get("sprint", "")
        updated += 1

    _save_tasks(existing)
    return jsonify({"ok": True, "created": created, "updated": updated})

@app.route("/api/jira/worklogs/<issue_key>")
def api_jira_worklogs(issue_key):
    from modules import jira_ops
    try:
        result = jira_ops.get_worklogs(issue_key)
        return jsonify({"ok": True, **result})
    except jira_ops.JiraError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/jira/worklogs/<issue_key>", methods=["POST"])
def api_jira_worklog_add(issue_key):
    from modules import jira_ops

    data = request.json or {}
    time_spent = (data.get("timeSpent") or "").strip()
    comment = (data.get("comment") or "").strip()
    if not time_spent:
        return jsonify({"ok": False, "error": "Informe o tempo gasto (ex: 2h, 1d, 30m)"}), 400

    try:
        jira_ops.add_worklog(issue_key, time_spent, comment)
        result = jira_ops.get_worklogs(issue_key)
        return jsonify({"ok": True, **result})
    except jira_ops.JiraError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>ZCRM Release</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
/* ── tokens ── */
:root {
  --accent:    #E96918;
  --accent2:   #C8530F;
  --accent-a:  rgba(233,105,24,.12);
  --bg:        #F0F4F9;
  --surface:   #FFFFFF;
  --surface2:  #F5F8FC;
  --border:    #DDE3EE;
  --text:      #111827;
  --text2:     #4B5B73;
  --text3:     #8EA3BF;
  --success:   #16A34A;
  --warn:      #D97706;
  --danger:    #DC2626;
  --info:      #2563EB;
  --purple:    #7C3AED;
  --sb-bg:     #0F1C2E;
  --sb-border: #1A2E48;
  --sb-text:   #B8CEDD;
  --sb-text2:  #5E7A96;
  --sb-active: rgba(233,105,24,.16);
  --sb-hover:  rgba(255,255,255,.05);
  --term-bg:   #0D1117;
  --term-fg:   #E6EDF3;
  --term-dim:  #8B949E;
  --radius:    8px;
  --shadow:    0 1px 3px rgba(0,0,0,.07), 0 4px 12px rgba(0,0,0,.04);
  --shadow-md: 0 4px 20px rgba(0,0,0,.1);
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --accent:   #F97320;
    --accent2:  #E96918;
    --accent-a: rgba(249,115,32,.12);
    --bg:       #0A1422;
    --surface:  #111C2D;
    --surface2: #162135;
    --border:   #1E3050;
    --text:     #E2E8F4;
    --text2:    #7A9BB8;
    --text3:    #3D5570;
    --shadow:   0 1px 3px rgba(0,0,0,.3), 0 4px 12px rgba(0,0,0,.2);
    --shadow-md:0 4px 24px rgba(0,0,0,.4);
  }
}
:root[data-theme="dark"] {
  --accent:   #F97320;
  --accent2:  #E96918;
  --accent-a: rgba(249,115,32,.12);
  --bg:       #0A1422;
  --surface:  #111C2D;
  --surface2: #162135;
  --border:   #1E3050;
  --text:     #E2E8F4;
  --text2:    #7A9BB8;
  --text3:    #3D5570;
  --shadow:   0 1px 3px rgba(0,0,0,.3), 0 4px 12px rgba(0,0,0,.2);
  --shadow-md:0 4px 24px rgba(0,0,0,.4);
}

/* ── reset ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
body{font-family:system-ui,-apple-system,sans-serif;font-size:14px;
     background:var(--bg);color:var(--text);min-height:100vh;display:flex;}
button{cursor:pointer;font:inherit;border:none;}
input,select,textarea{font:inherit;}
a{color:var(--accent);}
::-webkit-scrollbar{width:5px;height:5px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:99px;}

/* ── sidebar ── */
.sidebar{
  width:230px;min-width:230px;
  background:var(--sb-bg);
  border-right:1px solid var(--sb-border);
  display:flex;flex-direction:column;
  position:fixed;left:0;top:0;bottom:0;
  z-index:100;overflow-y:auto;
}
.sb-logo{
  padding:20px 18px 14px;
  display:flex;align-items:center;gap:10px;
  border-bottom:1px solid var(--sb-border);
}
.sb-logo-mark{
  width:32px;height:32px;border-radius:8px;
  background:var(--accent);
  display:flex;align-items:center;justify-content:center;
  font-size:14px;font-weight:900;color:#fff;flex-shrink:0;
}
.sb-logo-text{font-size:14px;font-weight:700;color:#fff;line-height:1.2;}
.sb-logo-sub{font-size:10px;color:var(--sb-text2);margin-top:1px;}

.sb-release{
  margin:12px 10px;
  background:rgba(233,105,24,.1);
  border:1px solid rgba(233,105,24,.25);
  border-radius:var(--radius);
  padding:10px 12px;
}
.sb-release-lbl{font-size:9px;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin-bottom:4px;}
.sb-release-val{font-size:11px;color:#fff;font-weight:600;word-break:break-all;line-height:1.4;}
.sb-release-sub{font-size:10px;color:var(--sb-text2);margin-top:3px;}

.sb-section-lbl{
  font-size:10px;text-transform:uppercase;letter-spacing:.08em;
  color:var(--sb-text2);padding:14px 18px 6px;
}
.sb-nav{list-style:none;padding:0 8px;}
.sb-item{
  display:flex;align-items:center;gap:10px;
  padding:9px 12px;border-radius:var(--radius);
  color:var(--sb-text);font-size:13px;font-weight:500;
  cursor:pointer;transition:background .12s,color .12s;
  margin-bottom:2px;position:relative;
}
.sb-item:hover{background:var(--sb-hover);color:#fff;}
.sb-item.active{background:var(--sb-active);color:var(--accent);}
.sb-item.active::before{
  content:'';position:absolute;left:-8px;top:50%;transform:translateY(-50%);
  width:3px;height:20px;background:var(--accent);border-radius:0 2px 2px 0;
}
.sb-icon{width:18px;text-align:center;font-size:15px;flex-shrink:0;}
.sb-badge{
  margin-left:auto;background:var(--danger);color:#fff;
  font-size:10px;font-weight:700;padding:1px 6px;border-radius:99px;
}

.sb-bottom{
  margin-top:auto;padding:14px 8px;
  border-top:1px solid var(--sb-border);
}
.sb-cfg-btn{
  display:flex;align-items:center;gap:10px;
  width:100%;padding:9px 12px;border-radius:var(--radius);
  background:none;color:var(--sb-text);font-size:13px;
  transition:background .12s;
}
.sb-cfg-btn:hover{background:var(--sb-hover);color:#fff;}

.sb-run-indicator{
  margin:0 10px 8px;padding:8px 12px;border-radius:var(--radius);
  background:rgba(217,119,6,.12);border:1px solid rgba(217,119,6,.3);
  display:none;
}
.sb-run-indicator.visible{display:block;}
.sb-run-dot{
  display:inline-block;width:7px;height:7px;border-radius:50%;
  background:var(--warn);margin-right:6px;
  animation:pulse 1s infinite;
}
.sb-run-txt{font-size:11px;color:var(--warn);font-weight:600;}

/* ── main ── */
.main{margin-left:230px;flex:1;min-height:100vh;display:flex;flex-direction:column;}

.topbar{
  height:52px;padding:0 24px;
  display:flex;align-items:center;gap:12px;
  background:var(--surface);border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:50;
  box-shadow:var(--shadow);
}
.topbar-title{font-size:16px;font-weight:700;color:var(--text);}
.topbar-sep{flex:1;}
.topbar-date{font-size:12px;color:var(--text3);}
.theme-btn{
  background:none;border:1px solid var(--border);border-radius:var(--radius);
  padding:5px 12px;color:var(--text2);font-size:12px;
}
.theme-btn:hover{border-color:var(--accent);color:var(--accent);}

.page-content{padding:24px;max-width:1200px;width:100%;}
#page-esteira  .page-content{max-width:none;}
#page-time     .page-content{max-width:none;}
#page-pipeline .page-content{max-width:none;}
#page-kpis     .page-content{max-width:none;}

.page{display:none;flex:1;}
.page.active{display:flex;flex-direction:column;}

/* ── overview ── */
.ov-greeting{margin-bottom:20px;}
.ov-greeting h1{font-size:22px;font-weight:700;color:var(--text);}
.ov-greeting p{font-size:13px;color:var(--text2);margin-top:3px;}

.stat-row{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:20px;}
@media(max-width:900px){.stat-row{grid-template-columns:repeat(2,1fr);}}
.stat-card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:16px 18px;
  box-shadow:var(--shadow);border-top:3px solid var(--border);
  transition:transform .12s;
}
.stat-card:hover{transform:translateY(-2px);}
.stat-card.c-all    {border-top-color:var(--text3);}
.stat-card.c-doing  {border-top-color:var(--info);}
.stat-card.c-review {border-top-color:var(--warn);}
.stat-card.c-blocked{border-top-color:var(--danger);}
.stat-card.c-done   {border-top-color:var(--success);}
.stat-num{font-size:28px;font-weight:800;color:var(--text);line-height:1;margin-bottom:4px;font-variant-numeric:tabular-nums;}
.stat-lbl{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;}

.ov-grid{display:grid;grid-template-columns:1fr 340px;gap:16px;margin-bottom:20px;}
@media(max-width:900px){.ov-grid{grid-template-columns:1fr;}}

.card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden;
}
.card-header{
  padding:12px 16px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:8px;
}
.card-header-title{font-size:13px;font-weight:700;color:var(--text);flex:1;}
.card-header-action{font-size:12px;color:var(--accent);cursor:pointer;background:none;padding:0;}
.card-header-action:hover{text-decoration:underline;}
.card-body{padding:16px;}

/* release status card */
.release-status{display:flex;flex-direction:column;gap:12px;}
.rs-row{display:flex;justify-content:space-between;align-items:center;}
.rs-key{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;}
.rs-val{font-size:12px;font-weight:600;color:var(--text);}
.rs-val.accent{color:var(--accent);}
.progress-bar{height:6px;background:var(--surface2);border-radius:99px;overflow:hidden;margin-top:4px;}
.progress-fill{height:100%;background:var(--success);border-radius:99px;transition:width .4s;}
.progress-fill.running{background:var(--warn);}
.progress-fill.failed{background:var(--danger);}
.rs-steps-summary{display:flex;gap:8px;flex-wrap:wrap;}
.rs-chip{font-size:10px;font-weight:600;padding:3px 8px;border-radius:99px;}
.rs-chip.s{background:#16A34A18;color:var(--success);}
.rs-chip.f{background:#DC262618;color:var(--danger);}
.rs-chip.p{background:var(--surface2);color:var(--text3);}
.rs-chip.r{background:#D9770618;color:var(--warn);}

/* sprint progress */
.sprint-prog{display:flex;flex-direction:column;gap:10px;}
.sp-col-row{display:flex;align-items:center;gap:10px;}
.sp-col-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
.sp-col-name{font-size:12px;color:var(--text2);width:90px;flex-shrink:0;}
.sp-bar-wrap{flex:1;height:8px;background:var(--surface2);border-radius:99px;overflow:hidden;}
.sp-bar{height:100%;border-radius:99px;transition:width .4s;}
.sp-count{font-size:11px;color:var(--text3);width:20px;text-align:right;font-variant-numeric:tabular-nums;}

/* team workload */
.team-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;}
.team-card{
  background:var(--surface);border:1px solid var(--border);
  border-radius:var(--radius);padding:14px;
  box-shadow:var(--shadow);display:flex;flex-direction:column;gap:10px;
  cursor:pointer;transition:border-color .12s,transform .12s;
}
.team-card:hover{border-color:var(--accent);transform:translateY(-2px);}
.team-card-top{display:flex;align-items:center;gap:10px;}
.avatar{
  width:36px;height:36px;border-radius:50%;
  background:var(--accent);color:#fff;
  display:flex;align-items:center;justify-content:center;
  font-size:13px;font-weight:700;flex-shrink:0;
}
.tc-name{font-size:13px;font-weight:700;color:var(--text);}
.tc-count{font-size:11px;color:var(--text3);}
.tc-bars{display:flex;flex-direction:column;gap:4px;}
.tc-bar-row{display:flex;align-items:center;gap:6px;}
.tc-bar-lbl{font-size:10px;color:var(--text3);width:72px;}
.tc-bar-wrap{flex:1;height:5px;background:var(--surface2);border-radius:99px;overflow:hidden;}
.tc-bar{height:100%;border-radius:99px;}
.tc-bar-n{font-size:10px;color:var(--text3);width:12px;text-align:right;font-variant-numeric:tabular-nums;}

/* ── pipeline ── */
.cfg-strip{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:12px 16px;display:flex;flex-wrap:wrap;gap:20px;align-items:center;
  margin-bottom:16px;box-shadow:var(--shadow);
}
.cfg-item{display:flex;flex-direction:column;gap:2px;}
.cfg-label{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);}
.cfg-value{font-size:13px;font-weight:600;color:var(--text);}
.cfg-value.accent{color:var(--accent);}
.cfg-sep{flex:1;}

.controls{display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap;align-items:center;}
.btn{
  border:none;border-radius:var(--radius);padding:9px 20px;
  font-size:13px;font-weight:600;transition:opacity .15s,transform .1s;
}
.btn:active{transform:scale(.97);}
.btn:disabled{opacity:.4;cursor:not-allowed;}
.btn-primary{background:var(--accent);color:#fff;}
.btn-primary:hover:not(:disabled){background:var(--accent2);}
.btn-danger{background:var(--danger);color:#fff;}
.btn-warning{background:var(--warning,#f59e0b);color:#fff;}
.btn-warning:hover:not(:disabled){opacity:.85;}
.btn-danger:hover:not(:disabled){opacity:.85;}
.btn-ghost{background:none;border:1px solid var(--border);color:var(--text2);}
.btn-ghost:hover{border-color:var(--accent);color:var(--accent);}

.run-status{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--text2);}
.status-dot{width:8px;height:8px;border-radius:50%;background:var(--text3);display:inline-block;}
.status-dot.running{background:var(--warn);animation:pulse 1s infinite;}
.status-dot.done-ok{background:var(--success);}
.status-dot.done-err{background:var(--danger);}
@keyframes pulse{0%,100%{opacity:1;}50%{opacity:.3;}}

.pipeline-grid{display:grid;grid-template-columns:1fr 300px;gap:16px;align-items:start;}
@media(max-width:800px){.pipeline-grid{grid-template-columns:1fr;}}

.terminal{background:var(--term-bg);border-radius:var(--radius);border:1px solid #30363D;overflow:hidden;box-shadow:var(--shadow);}
.term-header{display:flex;align-items:center;gap:8px;padding:9px 14px;background:#161B22;border-bottom:1px solid #30363D;}
.term-dot{width:10px;height:10px;border-radius:50%;}
.term-output{
  padding:14px;height:480px;overflow-y:auto;
  font-family:'Cascadia Code','Fira Code','Courier New',monospace;
  font-size:12.5px;line-height:1.6;color:var(--term-fg);
  white-space:pre-wrap;word-break:break-all;
}
.term-output:empty::before{content:'Aguardando execução…';color:var(--term-dim);font-style:italic;}
.term-line-err{color:#F85149;}
.term-line-warn{color:#E3B341;}
.term-line-ok{color:#3FB950;}
.term-line-dim{color:var(--term-dim);}

.sidebar-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow);}
.sidebar-title{font-size:11px;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);padding:10px 14px 8px;border-bottom:1px solid var(--border);}
.checklist-row{display:flex;align-items:flex-start;gap:8px;padding:8px 14px;font-size:12.5px;color:var(--text);cursor:pointer;border-bottom:1px solid var(--border);}
.checklist-row:last-child{border-bottom:none;}
.checklist-row input{margin-top:2px;flex-shrink:0;cursor:pointer;}
.checklist-row.done span{color:var(--text3);text-decoration:line-through;}
.step-list{list-style:none;}
.step-item{display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid var(--border);font-size:12px;}
.step-item:last-child{border-bottom:none;}
.step-icon{width:18px;height:18px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;flex-shrink:0;}
.si-success{background:rgba(22,163,74,.25);color:var(--success);}
.si-failed{background:rgba(220,38,38,.25);color:var(--danger);}
.si-running{background:rgba(217,119,6,.25);color:var(--warn);}
.si-pending{background:var(--surface2);color:var(--text3);}
.si-skipped{background:rgba(37,99,235,.18);color:var(--info);}
.step-name{flex:1;color:var(--text);}
.step-dur{font-size:10px;color:var(--text3);font-variant-numeric:tabular-nums;}

.docker-info{padding:12px 14px;display:flex;flex-direction:column;gap:8px;}
.docker-row{display:flex;flex-direction:column;gap:2px;}
.docker-lbl{font-size:10px;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);}
.docker-val{font-size:11px;color:var(--text);word-break:break-all;font-family:monospace;}

/* ── pipeline step cards ── */
.steps-main{display:flex;flex-direction:column;gap:5px;}
.step-card{
  background:var(--surface);border:1px solid var(--border);
  border-left:4px solid var(--border);
  border-radius:var(--radius);overflow:hidden;transition:border-color .2s,background .2s;
}
.step-card.sc-running{border-left-color:var(--warn);}
.step-card.sc-success{border-left-color:var(--success);}
.step-card.sc-failed {border-left-color:var(--danger);}
.step-card.sc-paused  {border-left-color:var(--warn);border-style:dashed;}
.step-card.sc-skipped {border-left-color:var(--text3);}
.si-skipped{background:var(--surface2);color:var(--text3);border:1px solid var(--border);}
.sc-header{
  display:flex;align-items:center;gap:10px;
  padding:10px 14px;cursor:pointer;user-select:none;
  transition:background .12s;
}
.sc-header:hover{background:var(--surface2);}
.step-card.sc-success .sc-header{background:rgba(22,163,74,.05);}
.step-card.sc-failed  .sc-header{background:rgba(220,38,38,.06);}
.step-card.sc-paused  .sc-header{background:rgba(217,119,6,.07);}
.sc-icon{
  width:24px;height:24px;border-radius:50%;
  display:flex;align-items:center;justify-content:center;
  font-size:11px;font-weight:700;flex-shrink:0;
}
.sc-name{flex:1;font-size:13px;font-weight:600;color:var(--text);}
.sc-dur{font-size:11px;color:var(--text3);font-variant-numeric:tabular-nums;margin-right:6px;}
.sc-log-btn{
  font-size:11px;color:var(--text3);background:none;
  border:1px solid var(--border);border-radius:4px;padding:2px 8px;
  transition:border-color .12s,color .12s;flex-shrink:0;
}
.sc-log-btn:hover{border-color:var(--accent);color:var(--accent);}
.sc-error{
  background:rgba(220,38,38,.06);border-top:1px solid rgba(220,38,38,.2);
  padding:8px 14px 10px 52px;font-size:12px;color:var(--danger);
  display:none;white-space:pre-wrap;word-break:break-word;
}
.sc-error.open{display:block;}
.sc-retry-btn{
  margin-top:6px;display:inline-block;font-size:11px;color:var(--warn);background:none;
  border:1px solid rgba(217,119,6,.4);border-radius:4px;padding:3px 10px;cursor:pointer;
}
.sc-retry-btn:hover{background:rgba(217,119,6,.1);}
.sc-log{
  display:none;background:var(--term-bg);
  padding:10px 14px;
  font-family:'Cascadia Code','Fira Code','Courier New',monospace;
  font-size:11.5px;line-height:1.55;color:var(--term-fg);
  max-height:200px;overflow-y:auto;
  white-space:pre-wrap;word-break:break-all;
}
.sc-log.open{display:block;}
.log-ok  {color:#4ade80;}
.log-err {color:#f87171;}
.log-info{color:#94a3b8;}
.log-warn{color:#fbbf24;}
@keyframes sc-spin{to{transform:rotate(360deg);}}
.sc-spinning{animation:sc-spin 1.2s linear infinite;display:inline-block;}

/* wizard modal */
.modal-wide{width:620px !important;}

/* ── kanban ── */
.kanban-toolbar{display:flex;gap:10px;margin-bottom:16px;flex-wrap:wrap;align-items:center;}
.kanban-toolbar select,.kanban-toolbar input{
  border:1px solid var(--border);border-radius:var(--radius);
  padding:7px 10px;background:var(--surface);color:var(--text);font-size:13px;
}
.kanban-board{display:grid;grid-template-columns:repeat(10,minmax(160px,1fr));gap:10px;align-items:start;overflow-x:auto;padding-bottom:12px;width:100%;}
.kanban-col{
  background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);
  min-height:180px;display:flex;flex-direction:column;transition:background .15s;
}
.kanban-col.drag-over{background:color-mix(in srgb,var(--accent) 8%,var(--surface2));border-color:var(--accent);}
.col-header{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--border);}
.col-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
.col-name{font-size:12px;font-weight:600;color:var(--text);flex:1;}
.col-count{font-size:11px;color:var(--text3);}
.col-body{padding:8px;display:flex;flex-direction:column;gap:8px;flex:1;}
.col-add{
  margin:8px;border:1px dashed var(--border);border-radius:var(--radius);
  padding:7px;font-size:12px;color:var(--text3);background:none;text-align:center;
}
.col-add:hover{border-color:var(--accent);color:var(--accent);}
.task-card{
  background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
  padding:10px 12px;cursor:grab;transition:box-shadow .15s,transform .15s;user-select:none;
}
.task-card:active{cursor:grabbing;}
.task-card:hover{box-shadow:var(--shadow);transform:translateY(-1px);}
.card-top{display:flex;align-items:flex-start;gap:6px;margin-bottom:6px;}
.card-title{font-size:13px;font-weight:600;color:var(--text);flex:1;word-break:break-word;line-height:1.4;}
.card-edit-btn{background:none;border:none;color:var(--text3);padding:2px 4px;font-size:13px;flex-shrink:0;border-radius:4px;}
.card-edit-btn:hover{color:var(--accent);background:var(--surface2);}
.card-meta{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px;}
.chip{font-size:10px;font-weight:600;padding:2px 7px;border-radius:99px;border:1px solid transparent;}
.chip-type-feat{background:#2563EB18;color:var(--info);border-color:#2563EB30;}
.chip-type-fix{background:#DC262618;color:var(--danger);border-color:#DC262630;}
.chip-type-chore{background:#D9770618;color:var(--warn);border-color:#D9770630;}
.chip-type-other{background:var(--surface2);color:var(--text3);border-color:var(--border);}
.chip-prio-high{background:#DC262618;color:var(--danger);}
.chip-prio-medium{background:#D9770618;color:var(--warn);}
.chip-prio-low{background:#16A34A18;color:var(--success);}
.card-footer{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--text3);}
.card-avatar{
  width:20px;height:20px;border-radius:50%;background:var(--accent);color:#fff;
  display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:700;flex-shrink:0;
}

/* ── time page ── */
.time-header{display:flex;align-items:center;gap:12px;margin-bottom:20px;flex-wrap:wrap;}
.time-filter{
  border:1px solid var(--border);border-radius:var(--radius);
  padding:7px 10px;background:var(--surface);color:var(--text);font-size:13px;
}
.member-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px;}
.member-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);box-shadow:var(--shadow);overflow:hidden;}
.member-card-top{padding:16px;display:flex;align-items:center;gap:14px;border-bottom:1px solid var(--border);}
.member-avatar{
  width:44px;height:44px;border-radius:50%;
  background:var(--accent);color:#fff;
  display:flex;align-items:center;justify-content:center;
  font-size:16px;font-weight:700;flex-shrink:0;
}
.member-name{font-size:15px;font-weight:700;color:var(--text);}
.member-stat{font-size:12px;color:var(--text2);margin-top:2px;}
.member-chips{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap;}
.member-chip{font-size:10px;font-weight:700;padding:3px 9px;border-radius:99px;}
.mc-doing{background:#2563EB18;color:var(--info);}
.mc-review{background:#D9770618;color:var(--warn);}
.mc-blocked{background:#DC262618;color:var(--danger);}
.mc-done{background:#16A34A18;color:var(--success);}
.mc-backlog{background:var(--surface2);color:var(--text3);}
.member-tasks{padding:12px 16px;display:flex;flex-direction:column;gap:8px;max-height:220px;overflow-y:auto;}
.mt-item{
  display:flex;align-items:center;gap:8px;
  padding:7px 10px;border-radius:6px;background:var(--surface2);
  cursor:pointer;transition:background .12s;
}
.mt-item:hover{background:var(--accent-a);}
.mt-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;}
.mt-title{font-size:12px;color:var(--text);flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;}
.mt-sprint{font-size:10px;color:var(--text3);}
.member-empty{padding:16px;font-size:12px;color:var(--text3);text-align:center;}

/* ── modal overlay (cfg) ── */
.modal-overlay{
  display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.55);z-index:200;
  align-items:center;justify-content:center;
}
.modal-overlay.open{display:flex;}
.modal{
  background:var(--surface);border-radius:12px;padding:24px;
  width:500px;max-width:calc(100vw - 32px);max-height:calc(100vh - 40px);
  overflow-y:auto;box-shadow:0 8px 40px rgba(0,0,0,.35);
}
.modal-title{font-size:16px;font-weight:700;color:var(--text);margin-bottom:18px;}
.form-row{margin-bottom:14px;}
.form-label{display:block;font-size:12px;font-weight:600;color:var(--text2);margin-bottom:5px;}
.form-control{
  width:100%;border:1px solid var(--border);border-radius:var(--radius);
  padding:8px 12px;background:var(--surface2);color:var(--text);font-size:13px;
}
.form-control:focus{outline:none;border-color:var(--accent);}
.form-row-2{display:grid;grid-template-columns:1fr 1fr;gap:12px;}
.modal-footer{display:flex;gap:10px;margin-top:20px;justify-content:flex-end;}
.btn-del{margin-right:auto;background:none;border:1px solid var(--danger);color:var(--danger);border-radius:var(--radius);padding:8px 16px;font-size:13px;}
.btn-del:hover{background:#DC262610;}

/* ── card detail modal ── */
.cd-overlay{
  display:none;position:fixed;inset:0;
  background:rgba(0,0,0,.6);z-index:300;
  align-items:flex-start;justify-content:center;
  padding:32px 16px;overflow-y:auto;
}
.cd-overlay.open{display:flex;}
.cd-card{
  background:var(--surface);border-radius:12px;
  width:860px;max-width:100%;
  box-shadow:0 12px 60px rgba(0,0,0,.4);
  display:flex;flex-direction:column;
  margin:auto;
}
.cd-header{
  display:flex;align-items:center;gap:10px;
  padding:14px 20px;border-bottom:1px solid var(--border);
  flex-wrap:wrap;gap:8px;
}
.cd-status-select{
  border:none;border-radius:6px;padding:5px 10px;
  font-size:12px;font-weight:700;cursor:pointer;
  background:var(--surface2);color:var(--text);
  border:1px solid var(--border);
}
.cd-header-sep{flex:1;}
.cd-close{
  background:none;border:none;color:var(--text3);
  font-size:18px;padding:4px 8px;border-radius:6px;line-height:1;
}
.cd-close:hover{background:var(--surface2);color:var(--text);}
.cd-body{display:grid;grid-template-columns:1fr 260px;min-height:0;}
@media(max-width:700px){.cd-body{grid-template-columns:1fr;}}
.cd-main{
  padding:20px 24px;border-right:1px solid var(--border);
  display:flex;flex-direction:column;gap:16px;
}
.cd-title-input{
  width:100%;border:none;background:transparent;
  font-size:20px;font-weight:700;color:var(--text);
  resize:none;outline:none;line-height:1.35;
  font-family:inherit;padding:0;
}
.cd-title-input::placeholder{color:var(--text3);}
.cd-title-input:focus{border-bottom:2px solid var(--accent);}
.cd-section-lbl{
  font-size:11px;font-weight:700;text-transform:uppercase;
  letter-spacing:.06em;color:var(--text3);margin-bottom:6px;
}
.cd-chips-row{display:flex;flex-wrap:wrap;gap:6px;align-items:center;}
.cd-notes{
  width:100%;border:1px solid var(--border);border-radius:var(--radius);
  background:var(--surface2);color:var(--text);font-size:13px;
  padding:10px 12px;resize:vertical;min-height:120px;outline:none;
  font-family:inherit;line-height:1.6;
}
.cd-notes:focus{border-color:var(--accent);}
.cd-sidebar{
  padding:20px 16px;display:flex;flex-direction:column;gap:14px;
  background:var(--surface2);border-radius:0 0 12px 0;
}
.cd-field{display:flex;flex-direction:column;gap:4px;}
.cd-field-lbl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);}
.cd-field-input{
  border:1px solid var(--border);border-radius:6px;
  padding:7px 10px;background:var(--surface);color:var(--text);font-size:13px;
  width:100%;outline:none;
}
.cd-field-input:focus{border-color:var(--accent);}
.cd-jira-link{
  display:flex;align-items:center;gap:6px;
}
.cd-jira-link a{font-size:12px;color:var(--info);text-decoration:none;}
.cd-jira-link a:hover{text-decoration:underline;}
.cd-footer{
  display:flex;gap:10px;padding:14px 20px;
  border-top:1px solid var(--border);justify-content:flex-end;
  align-items:center;
}
.cd-del{margin-right:auto;background:none;border:1px solid var(--danger);color:var(--danger);border-radius:var(--radius);padding:7px 14px;font-size:13px;}
.cd-del:hover{background:#DC262610;}
.chip-sel{
  border:none;background:transparent;font-size:11px;font-weight:700;
  cursor:pointer;padding:0;color:inherit;
}
/* ── KPI page ─────────────────────────────────────────────────────────────── */
.kpi-header{display:flex;align-items:center;gap:12px;margin-bottom:20px;}
.kpi-metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:28px;}
@media(max-width:900px){.kpi-metrics{grid-template-columns:repeat(3,1fr);}}
.kpi-metric{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px 14px;text-align:center;}
.kpi-metric-num{font-size:30px;font-weight:700;color:var(--text);line-height:1;font-variant-numeric:tabular-nums;}
.kpi-metric-lbl{font-size:11px;color:var(--text3);margin-top:6px;text-transform:uppercase;letter-spacing:.05em;}
.kpi-section{margin-bottom:28px;}
.kpi-section-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid var(--border);}
.kpi-devs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:12px;}
.kpi-dev-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px;}
.kpi-dev-top{display:flex;align-items:center;gap:10px;margin-bottom:10px;}
.kpi-dev-avatar{width:38px;height:38px;border-radius:50%;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-size:14px;font-weight:700;flex-shrink:0;}
.kpi-dev-info{flex:1;min-width:0;}
.kpi-dev-name{font-size:14px;font-weight:600;color:var(--text);}
.kpi-dev-sub{font-size:11px;color:var(--text3);margin-top:1px;}
.kpi-dev-rate{font-size:22px;font-weight:700;flex-shrink:0;}
.kpi-rate-good{color:var(--success);}
.kpi-rate-mid{color:var(--warn);}
.kpi-rate-low{color:var(--danger);}
.kpi-progress-bar{height:5px;background:var(--border);border-radius:3px;overflow:hidden;margin-bottom:8px;}
.kpi-progress-fill{height:100%;border-radius:3px;transition:width .4s;}
.kpi-type-chips{display:flex;flex-wrap:wrap;gap:4px;}
.kpi-type-chip{font-size:10px;font-weight:600;padding:2px 8px;border-radius:10px;}
.kpi-hist-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border);}
.kpi-hist-sprint{width:90px;font-size:12px;font-weight:600;color:var(--text);flex-shrink:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.kpi-hist-bar-wrap{flex:1;height:8px;background:var(--border);border-radius:4px;overflow:hidden;}
.kpi-hist-bar-fill{height:100%;border-radius:4px;transition:width .5s;}
.kpi-hist-nums{width:44px;text-align:right;font-size:11px;color:var(--text3);flex-shrink:0;}
.kpi-hist-rate{width:36px;text-align:right;font-size:12px;font-weight:700;flex-shrink:0;}
.kpi-type-row{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border);}
.kpi-type-lbl{width:110px;font-size:12px;color:var(--text);flex-shrink:0;}
.kpi-type-bar-wrap{flex:1;height:8px;background:var(--border);border-radius:4px;overflow:hidden;}
.kpi-type-bar-fill{height:100%;border-radius:4px;transition:width .5s;}
.kpi-type-cnt{width:26px;text-align:right;font-size:12px;color:var(--text3);flex-shrink:0;}
.kpi-type-pct{width:34px;text-align:right;font-size:12px;font-weight:600;color:var(--text);flex-shrink:0;}
.kpi-two-col{display:grid;grid-template-columns:1fr 1fr;gap:24px;}
@media(max-width:760px){.kpi-two-col{grid-template-columns:1fr;}}

/* ── comparativo entre releases ── */
.kpi-cmp-header{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:12px;margin-bottom:18px;}
.kpi-cmp-title{font-size:18px;font-weight:700;color:var(--text);}
.kpi-cmp-selects{display:flex;align-items:center;gap:10px;}
.kpi-cmp-select-group{display:flex;flex-direction:column;gap:3px;}
.kpi-cmp-select-group label{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;}
.kpi-cmp-arrow{color:var(--text3);font-size:16px;margin-top:14px;}
.kpi-cmp-cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px;}
@media(max-width:900px){.kpi-cmp-cards{grid-template-columns:repeat(2,1fr);}}
.kpi-cmp-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px;border-top:3px solid var(--border);}
.kpi-cmp-card-lbl{font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:10px;}
.kpi-cmp-card-vals{display:flex;align-items:baseline;gap:8px;}
.kpi-cmp-card-before{font-size:18px;color:var(--text3);}
.kpi-cmp-card-arrow{color:var(--text3);font-size:14px;}
.kpi-cmp-card-after{font-size:26px;font-weight:700;color:var(--text);}
.kpi-cmp-card-delta{display:inline-block;margin-top:10px;font-size:11px;font-weight:700;padding:3px 9px;border-radius:12px;}
.kpi-cmp-chart-group{margin-bottom:22px;}
.kpi-cmp-chart-group:last-child{margin-bottom:0;}
.kpi-cmp-chart-title{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);margin-bottom:10px;}
.kpi-cmp-bars{display:flex;align-items:flex-end;gap:18px;height:120px;padding:0 10px;}
.kpi-cmp-bar-col{display:flex;flex-direction:column;align-items:center;justify-content:flex-end;flex:1;height:100%;}
.kpi-cmp-bar-num{font-size:14px;font-weight:700;color:var(--text);margin-bottom:6px;}
.kpi-cmp-bar{width:36px;border-radius:5px 5px 0 0;transition:height .4s;min-height:3px;}
.kpi-cmp-bar-lbl{font-size:10px;color:var(--text3);margin-top:6px;}
.kpi-cmp-delta-row{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;margin-bottom:10px;}
.kpi-cmp-delta-lbl{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px;}
.kpi-cmp-delta-vals{font-size:16px;font-weight:700;color:var(--text);}
.kpi-cmp-delta-sub{font-size:11px;margin-top:3px;}
.kpi-cmp-equation{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px;font-size:13px;color:var(--text);display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.kpi-cmp-insights-title{font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin:14px 0 8px;}
.kpi-cmp-insight{font-size:12.5px;color:var(--text);padding:4px 0;}
.kpi-cmp-summary-row{font-size:12.5px;color:var(--text);padding:6px 0;border-bottom:1px solid var(--border);}
.kpi-cmp-summary-row:last-child{border-bottom:none;}
.kpi-cmp-summary-row b{color:var(--accent);}
/* ── KPI page hero header ── */
.kpi-page-hero{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:16px;margin-bottom:26px;padding-bottom:22px;border-bottom:1px solid var(--border);}
.kpi-hero-left{}
.kpi-hero-brand{font-size:10px;font-weight:800;letter-spacing:.2em;text-transform:uppercase;color:var(--accent);margin-bottom:5px;}
.kpi-hero-title{font-size:26px;font-weight:800;color:var(--text);line-height:1.1;margin:0 0 5px;}
.kpi-hero-sub{font-size:12px;color:var(--text3);}
.kpi-hero-right{display:flex;flex-direction:column;align-items:flex-end;gap:10px;}
.kpi-release-tabs{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;}
.kpi-release-tab{border-radius:8px;padding:9px 16px;display:flex;flex-direction:column;gap:2px;min-width:110px;}
.kpi-rtab-base{background:var(--surface2);border:1px solid var(--border);}
.kpi-rtab-cur{background:rgba(6,182,212,.08);border:1px solid rgba(6,182,212,.3);}
.kpi-rtab-evo{background:var(--surface2);border:1px solid var(--border);}
.kpi-rtab-release{font-size:14px;font-weight:700;color:var(--text);}
.kpi-rtab-role{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.09em;color:var(--text3);}
.kpi-rtab-cur .kpi-rtab-release{color:var(--accent);}
.kpi-rtab-cur .kpi-rtab-role{color:var(--accent);opacity:.8;}
.kpi-rtab-evo .kpi-rtab-role{color:var(--success);}
.kpi-rtab-narrative{font-size:11px;color:var(--success);font-weight:600;margin-top:1px;}
.kpi-cmp-selects-row{display:flex;align-items:center;gap:10px;}
/* ── Formalizar page ──────────────────────────────────────────────────────── */
.fz-page-header{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:14px;margin-bottom:22px;padding-bottom:18px;border-bottom:1px solid var(--border);}
.fz-page-title{font-size:22px;font-weight:800;color:var(--text);margin:0 0 4px;}
.fz-spec-badge{display:inline-flex;align-items:center;gap:10px;background:var(--surface2);border:1px solid var(--border);border-radius:20px;padding:5px 14px;font-size:11px;color:var(--text3);margin-top:4px;}
.fz-spec-badge b{color:var(--accent);font-weight:700;}
.fz-add-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px;margin-bottom:24px;}
.fz-add-box-title{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:14px;}
.fz-row{display:flex;gap:10px;margin-bottom:10px;flex-wrap:wrap;}
.fz-field{display:flex;flex-direction:column;gap:4px;}
.fz-field label{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);}
.fz-field-sm{width:160px;}
.fz-field-md{width:220px;}
.fz-field-grow{flex:1;min-width:200px;}
.fz-branch-row{display:flex;gap:6px;}
.fz-branch-row select{flex:1;}
.fz-or{font-size:11px;color:var(--text3);padding:0 4px;align-self:center;margin-top:14px;}
.fz-add-footer{display:flex;justify-content:flex-end;margin-top:14px;}
.fz-section-hdr{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:12px;padding-bottom:6px;border-bottom:1px solid var(--border);}
.fz-empty{text-align:center;padding:40px 0;color:var(--text3);font-size:13px;}
.fz-item{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:14px 16px;margin-bottom:8px;display:flex;align-items:flex-start;gap:12px;}
.fz-item-left{flex:1;min-width:0;}
.fz-item-top{display:flex;align-items:center;gap:8px;margin-bottom:6px;flex-wrap:wrap;}
.fz-badge{font-size:10px;font-weight:700;padding:2px 9px;border-radius:10px;text-transform:uppercase;letter-spacing:.06em;}
.fz-badge-pending{background:var(--surface2);color:var(--text3);border:1px solid var(--border);}
.fz-badge-sent{background:rgba(34,197,94,.12);color:var(--success);border:1px solid rgba(34,197,94,.25);}
.fz-badge-error{background:rgba(239,68,68,.12);color:var(--danger);border:1px solid rgba(239,68,68,.25);}
.fz-badge-sending{background:rgba(6,182,212,.12);color:var(--accent);border:1px solid rgba(6,182,212,.25);}
.fz-badge-duplicate{background:rgba(245,158,11,.12);color:var(--warning,#f59e0b);border:1px solid rgba(245,158,11,.25);}
.fz-btn-del-slack{background:rgba(239,68,68,.08);color:var(--danger);border:1px solid rgba(239,68,68,.2);font-size:11px;padding:3px 8px;border-radius:5px;cursor:pointer;}
.fz-btn-del-slack:hover{background:rgba(239,68,68,.18);}
.fz-badge-type{background:var(--surface2);color:var(--text2);border:1px solid var(--border);}
.fz-task{font-size:13px;font-weight:700;color:var(--accent);}
.fz-branch{font-size:12px;color:var(--text2);font-family:monospace;margin-bottom:3px;word-break:break-all;}
.fz-conteudo{font-size:12px;color:var(--text3);}
.fz-item-error{font-size:11px;color:var(--danger);margin-top:4px;}
.fz-item-actions{display:flex;flex-direction:column;gap:6px;flex-shrink:0;}
.fz-btn{font-size:11px;font-weight:600;padding:5px 12px;border-radius:6px;border:1px solid var(--border);cursor:pointer;background:var(--surface2);color:var(--text2);transition:background .15s;}
.fz-btn:hover{background:var(--accent);color:#fff;border-color:var(--accent);}
.fz-btn-send{background:var(--accent);color:#fff;border-color:var(--accent);}
.fz-btn-send:hover{opacity:.85;}
.fz-btn-del{color:var(--danger);}
.fz-btn-del:hover{background:var(--danger);color:#fff;border-color:var(--danger);}
.fz-send-all-btn{background:var(--accent);color:#fff;border:none;padding:8px 18px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;}
.fz-send-all-btn:hover{opacity:.85;}

/* ── modal ─────────────────────────────────────────────────────────────────── */
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9000;display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:opacity .15s;}
.modal-backdrop.open{opacity:1;pointer-events:all;}
.modal-box{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:28px 28px 20px;width:clamp(300px,90vw,480px);box-shadow:var(--shadow-md);transform:translateY(8px);transition:transform .15s;}
.modal-backdrop.open .modal-box{transform:translateY(0);}
.modal-icon{font-size:28px;margin-bottom:10px;}
.modal-title{font-size:15px;font-weight:700;color:var(--text);margin-bottom:6px;}
.modal-msg{font-size:13px;color:var(--text2);line-height:1.55;white-space:pre-wrap;}
.modal-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:20px;}
.modal-btn{padding:7px 18px;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;border:1px solid var(--border);background:var(--surface2);color:var(--text);transition:background .12s;}
.modal-btn:hover{background:var(--border);}
.modal-btn.primary{background:var(--accent);color:#fff;border-color:var(--accent);}
.modal-btn.primary:hover{opacity:.85;}
.modal-btn.danger{background:var(--danger);color:#fff;border-color:var(--danger);}
.modal-btn.danger:hover{opacity:.85;}
.modal-field{margin-bottom:14px;}
.modal-field label{display:block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin-bottom:5px;}
.modal-field input{width:100%;padding:7px 9px;border-radius:6px;border:1px solid var(--border);background:var(--surface2);color:var(--text);font-size:13px;box-sizing:border-box;}
.modal-field input:focus{outline:2px solid var(--accent);border-color:transparent;}

/* ── formalizar: reply spec + bulk + preview ─────────────────────────────── */
.fz-reply-box{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px;margin-bottom:18px;}
.fz-reply-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:8px;}
.fz-reply-ta{width:100%;background:var(--surface2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-family:monospace;font-size:12px;line-height:1.6;resize:vertical;min-height:78px;}
.fz-reply-ta:focus{outline:none;border-color:var(--accent);}
.fz-reply-hint{font-size:11px;color:var(--text3);margin-top:5px;text-align:right;min-height:14px;}
.fz-bulk-toggle{font-size:12px;color:var(--accent);cursor:pointer;background:none;border:none;padding:0;margin-bottom:8px;text-decoration:underline;}
.fz-bulk-box{background:var(--surface2);border:1px dashed var(--border);border-radius:var(--radius);padding:14px;margin-top:10px;}
.fz-bulk-ta{width:100%;background:var(--surface);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:8px 10px;font-family:monospace;font-size:12px;resize:vertical;min-height:90px;}
.fz-bulk-ta:focus{outline:none;border-color:var(--accent);}
.fz-bulk-hint{font-size:11px;color:var(--text3);margin:6px 0 10px;}
.fz-preview{margin-top:8px;border-top:1px solid var(--border);padding-top:8px;}
.fz-preview summary{font-size:11px;color:var(--text3);cursor:pointer;user-select:none;list-style:none;}
.fz-preview summary::marker,.fz-preview summary::-webkit-details-marker{display:none;}
.fz-preview summary::before{content:'▸ ';font-size:10px;}
details.fz-preview[open] summary::before{content:'▾ ';}
.fz-preview-msg{margin-top:6px;background:var(--term-bg);color:var(--term-fg);border-radius:6px;padding:8px 10px;font-family:monospace;font-size:11px;line-height:1.6;white-space:pre-wrap;word-break:break-all;}
.fz-preview-sep{text-align:center;font-size:10px;color:var(--text3);margin:4px 0;}
.fz-preview-reply{background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-family:monospace;font-size:11px;color:var(--text2);white-space:pre-wrap;}

/* ── history page ── */
#page-historico .page-content{max-width:none;}
.hist-empty{text-align:center;padding:48px 0;color:var(--text3);font-size:13px;}
.hist-table{width:100%;border-collapse:collapse;font-size:13px;}
.hist-table th{text-align:left;padding:8px 12px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);border-bottom:2px solid var(--border);background:var(--surface2);}
.hist-table td{padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:middle;}
.hist-table tr:last-child td{border-bottom:none;}
.hist-table tr:hover td{background:var(--surface2);}
.hist-outcome{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:700;padding:3px 9px;border-radius:99px;}
.hist-outcome.success{background:rgba(22,163,74,.12);color:var(--success);}
.hist-outcome.failed{background:rgba(220,38,38,.12);color:var(--danger);}
.hist-pipeline{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;padding:2px 8px;border-radius:99px;}
.hist-pl-release{background:rgba(37,99,235,.12);color:var(--info);}
.hist-pl-hotfix{background:rgba(220,38,38,.12);color:var(--danger);}
.hist-note{font-size:10px;color:var(--text3);font-style:italic;}

/* ── DORA metrics ── */
.dora-row{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px;}
@media(max-width:900px){.dora-row{grid-template-columns:repeat(2,1fr);}}
.dora-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);padding:18px 20px;box-shadow:var(--shadow);position:relative;overflow:hidden;}
.dora-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;}
.dora-card.dc-df::before{background:var(--success);}
.dora-card.dc-lt::before{background:var(--info);}
.dora-card.dc-cfr::before{background:var(--warn);}
.dora-card.dc-mttr::before{background:var(--purple);}
.dora-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text3);margin-bottom:6px;}
.dora-val{font-size:30px;font-weight:800;color:var(--text);line-height:1;margin-bottom:3px;font-variant-numeric:tabular-nums;}
.dora-unit{font-size:11px;color:var(--text3);margin-bottom:10px;}
.dora-badge{display:inline-flex;align-items:center;font-size:10px;font-weight:700;padding:2px 8px;border-radius:99px;letter-spacing:.04em;}
.dora-badge.elite{background:rgba(22,163,74,.14);color:var(--success);}
.dora-badge.high{background:rgba(37,99,235,.12);color:var(--info);}
.dora-badge.medium{background:rgba(217,119,6,.12);color:var(--warn);}
.dora-badge.low{background:rgba(220,38,38,.12);color:var(--danger);}
</style>
</head>
<body>

<!-- ══ MODAL ═════════════════════════════════════════════════════════════════ -->
<div id="app-modal" class="modal-backdrop" role="dialog" aria-modal="true">
  <div class="modal-box">
    <div id="modal-icon"  class="modal-icon"></div>
    <div id="modal-title" class="modal-title"></div>
    <div id="modal-msg"   class="modal-msg"></div>
    <div id="modal-actions" class="modal-actions"></div>
  </div>
</div>

<!-- ══ SIDEBAR ══════════════════════════════════════════════════════════════ -->
<nav class="sidebar">
  <div class="sb-logo">
    <div class="sb-logo-mark">Z</div>
    <div>
      <div class="sb-logo-text">ZCRM</div>
      <div class="sb-logo-sub">Release Dashboard</div>
    </div>
  </div>

  <div class="sb-release" id="sb-release-box">
    <div class="sb-release-lbl">Release ativa</div>
    <div class="sb-release-val" id="sb-rel-branch">—</div>
    <div class="sb-release-sub" id="sb-rel-meta">—</div>
  </div>

  <div class="sb-run-indicator" id="sb-running">
    <span class="sb-run-dot"></span>
    <span class="sb-run-txt">Pipeline rodando…</span>
  </div>

  <div class="sb-section-lbl">Navegação</div>
  <ul class="sb-nav">
    <li class="sb-item active" onclick="goto('overview', this)">
      <span class="sb-icon">⊞</span> Visão Geral
    </li>
    <li class="sb-item" onclick="goto('pipeline', this)">
      <span class="sb-icon">▶</span> Pipeline
    </li>
    <li class="sb-item" onclick="goto('hotfix', this)">
      <span class="sb-icon">✚</span> Hotfix
    </li>
    <li class="sb-item" onclick="goto('esteira', this)">
      <span class="sb-icon">▦</span> Esteira do Time
    </li>
    <li class="sb-item" onclick="goto('time', this)">
      <span class="sb-icon">◎</span> Time
    </li>
    <li class="sb-item" onclick="goto('kpis', this)">
      <span class="sb-icon">◈</span> KPIs do Time
    </li>
    <li class="sb-item" onclick="goto('formalizar', this)">
      <span class="sb-icon">↗</span> Formalizar
    </li>
    <li class="sb-item" onclick="goto('historico', this)">
      <span class="sb-icon">◷</span> Histórico
    </li>
  </ul>

  <div class="sb-bottom">
    <button class="sb-cfg-btn" onclick="openCfgModal()">
      <span class="sb-icon">⚙</span> Configurar Release
    </button>
    <button class="sb-cfg-btn" onclick="toggleTheme()" style="margin-top:2px;">
      <span class="sb-icon">◑</span> Alternar tema
    </button>
  </div>
</nav>

<!-- ══ MAIN ═════════════════════════════════════════════════════════════════ -->
<div class="main">
  <header class="topbar">
    <span class="topbar-title" id="topbar-title">Visão Geral</span>
    <div class="topbar-sep"></div>
    <span class="topbar-date" id="topbar-date"></span>
  </header>

  <!-- ── VISÃO GERAL ─────────────────────────────────────────────────────── -->
  <div id="page-overview" class="page active">
    <div class="page-content">

      <div class="ov-greeting">
        <h1>Olá, Gabriel 👋</h1>
        <p id="ov-sprint-lbl">Carregando sprint…</p>
      </div>

      <div class="stat-row">
        <div class="stat-card c-all">
          <div class="stat-num" id="s-total">0</div>
          <div class="stat-lbl">Total de tasks</div>
        </div>
        <div class="stat-card c-doing">
          <div class="stat-num" id="s-doing">0</div>
          <div class="stat-lbl">Em Andamento</div>
        </div>
        <div class="stat-card c-review">
          <div class="stat-num" id="s-review">0</div>
          <div class="stat-lbl">Aguard. Autorização</div>
        </div>
        <div class="stat-card c-blocked">
          <div class="stat-num" id="s-blocked">0</div>
          <div class="stat-lbl">Em validação</div>
        </div>
        <div class="stat-card c-done">
          <div class="stat-num" id="s-done">0</div>
          <div class="stat-lbl">Concluídas</div>
        </div>
      </div>

      <div class="ov-grid">

        <!-- sprint progress -->
        <div class="card">
          <div class="card-header">
            <span class="card-header-title">Progresso da Sprint</span>
            <button class="card-header-action" onclick="goto('esteira', document.querySelector('.sb-item:nth-child(3)'))">Ver board →</button>
          </div>
          <div class="card-body">
            <div class="sprint-prog" id="sprint-prog"></div>
          </div>
        </div>

        <!-- release status -->
        <div class="card">
          <div class="card-header">
            <span class="card-header-title">Status da Release</span>
            <button class="card-header-action" onclick="gotoByName('pipeline')">Ver pipeline →</button>
          </div>
          <div class="card-body">
            <div class="release-status" id="release-status"></div>
          </div>
        </div>

      </div>

      <!-- team workload -->
      <div class="card">
        <div class="card-header">
          <span class="card-header-title">Carga do Time</span>
          <button class="card-header-action" onclick="gotoByName('time')">Ver detalhes →</button>
        </div>
        <div class="card-body">
          <div class="team-grid" id="team-workload"></div>
        </div>
      </div>

      <!-- kpi snapshot -->
      <div class="card">
        <div class="card-header">
          <span class="card-header-title">KPIs da Sprint</span>
          <button class="card-header-action" onclick="gotoByName('kpis')">Ver KPIs →</button>
        </div>
        <div class="card-body">
          <div id="ov-kpi-snap" style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;"></div>
        </div>
      </div>

      <!-- pipeline activity -->
      <div class="card" style="margin-top:16px;">
        <div class="card-header">
          <span class="card-header-title">Atividade recente — Pipelines</span>
          <button class="card-header-action" onclick="gotoByName('historico')">Ver histórico →</button>
        </div>
        <div id="ov-pipeline-activity" style="padding:0 4px 4px;">
          <div style="padding:14px 12px;font-size:13px;color:var(--text3)">Nenhuma execução registrada.</div>
        </div>
      </div>

    </div>
  </div>

  <!-- ── PIPELINE ────────────────────────────────────────────────────────── -->
  <div id="page-pipeline" class="page">
    <div class="page-content">

      <div class="cfg-strip">
        <div class="cfg-item">
          <span class="cfg-label">Branch</span>
          <span class="cfg-value accent" id="ci-branch">—</span>
        </div>
        <div class="cfg-item">
          <span class="cfg-label">Versão</span>
          <span class="cfg-value" id="ci-version">—</span>
        </div>
        <div class="cfg-item">
          <span class="cfg-label">CRM</span>
          <span class="cfg-value" id="ci-crm">—</span>
        </div>
        <div class="cfg-item">
          <span class="cfg-label">Próxima release</span>
          <span class="cfg-value" id="ci-next">—</span>
        </div>
        <div class="cfg-sep"></div>
        <button class="btn btn-ghost" onclick="openCfgModal()" style="font-size:12px;padding:7px 14px;">⚙ Configurar</button>
      </div>

      <div class="controls">
        <button class="btn btn-primary" id="btn-run"      onclick="startRelease()">▶ Iniciar Release</button>
        <button class="btn btn-warning" id="btn-pause"    onclick="togglePause()" disabled>⏸ Pausar</button>
        <button class="btn btn-danger"  id="btn-stop"     onclick="stopRelease()" disabled>■ Parar</button>
        <button class="btn" id="btn-rollback" onclick="showRollbackModal()" style="background:var(--surface2);color:var(--text2);border:1px solid var(--border);margin-left:6px" title="Reverter commits locais da pipeline">↩ Reverter</button>
        <button class="btn btn-ghost"   onclick="clearTerminal()">Limpar</button>
        <div class="run-status">
          <span class="status-dot" id="run-dot"></span>
          <span id="run-label">Ocioso</span>
        </div>
        <span id="run-timer" style="font-size:12px;color:var(--text3);font-variant-numeric:tabular-nums;display:none;margin-left:4px;">⏱ 0:00</span>
        <span id="run-estimate" style="font-size:11px;color:var(--text3);display:none;margin-left:2px;"></span>
      </div>

      <div class="pipeline-grid">
        <div class="steps-main" id="steps-main"></div>
        <div style="display:flex;flex-direction:column;gap:12px;min-width:0">
          <div class="sidebar-card">
            <div class="sidebar-title">Progresso</div>
            <div class="docker-info" id="pipeline-progress"></div>
          </div>
          <div class="sidebar-card">
            <div class="sidebar-title">Docker</div>
            <div class="docker-info" id="docker-info"></div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ── HOTFIX ──────────────────────────────────────────────────────────── -->
  <div id="page-hotfix" class="page">
    <div class="page-content">

      <div class="card" style="margin-bottom:16px;">
        <div class="card-header">
          <span class="card-header-title">Dados do hotfix</span>
        </div>
        <div class="card-body">
          <div class="form-row-2">
            <div class="form-row">
              <label class="form-label">Branch de hotfix</label>
              <input class="form-control" id="hz-branch" placeholder="hotfix/NEOSDEV-1234-corrige-login">
            </div>
            <div class="form-row">
              <label class="form-label">Branch de release alvo (onde o hotfix entra primeiro)</label>
              <input class="form-control" id="hz-target" placeholder="release/v2.30.0_zcrm_api_2.30.0_RC">
            </div>
          </div>
          <div class="form-row-2">
            <div class="form-row">
              <label class="form-label">Versão do hotfix</label>
              <input class="form-control" id="hz-version" placeholder="v2.30.4_zcrm_api_2.30.0" oninput="renderHotfixChecklist()">
            </div>
            <div class="form-row">
              <label class="form-label">Chave do JIRA</label>
              <input class="form-control" id="hz-ticket" placeholder="NEOSDEV-1234 (vazio detecta da branch)">
            </div>
          </div>
          <div class="form-row">
            <label class="form-label">Descrição para o CHANGELOG</label>
            <input class="form-control" id="hz-description" placeholder="vazio gera a partir do nome da branch">
          </div>
          <div class="form-row">
            <label class="form-label" style="display:flex;align-items:center;gap:8px;cursor:pointer;">
              <input type="checkbox" id="hz-previous" onchange="toggleHotfixPreviousFields()">
              Este hotfix é de uma release ANTERIOR à release atualmente em teste
            </label>
          </div>
          <div class="form-row" id="hz-current-release-row" style="display:none;">
            <label class="form-label">Branch da release ATUAL (recebe a propagação do fix)</label>
            <input class="form-control" id="hz-current-release" placeholder="release/v2.31.0_zcrm_api_2.31.0_RC">
          </div>
        </div>
      </div>

      <div style="display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap;margin-bottom:16px;">
        <div class="form-row" style="margin:0;min-width:260px;flex:1;">
          <label class="form-label">Iniciar a partir da etapa</label>
          <select class="form-control" id="hz-from-step">
            <option value="1">01 — Do início (completo)</option>
            <option value="2">02 — Checkout da release alvo</option>
            <option value="3">03 — Merge da branch de hotfix</option>
            <option value="4">04 — Atualizar CHANGELOG</option>
            <option value="5">05 — Atualizar package.json</option>
            <option value="6">06 — ng build --prod</option>
            <option value="7">07 — Criar tag do hotfix</option>
            <option value="8">08 — Propagar para release atual</option>
            <option value="9">09 — Docker build + push</option>
            <option value="10">10 — Relatório final</option>
          </select>
        </div>
      </div>

      <div class="controls">
        <button class="btn btn-primary" id="btn-hotfix-run"   onclick="startHotfix()">▶ Iniciar Hotfix</button>
        <button class="btn btn-warning" id="btn-hotfix-pause" onclick="togglePause()" disabled>⏸ Pausar</button>
        <button class="btn btn-danger"  id="btn-hotfix-stop"  onclick="stopHotfix()" disabled>■ Parar</button>
        <div class="run-status">
          <span class="status-dot" id="hotfix-run-dot"></span>
          <span id="hotfix-run-label">Ocioso</span>
        </div>
        <span id="hotfix-run-timer" style="font-size:12px;color:var(--text3);font-variant-numeric:tabular-nums;display:none;margin-left:4px;">⏱ 0:00</span>
        <span id="hotfix-run-estimate" style="font-size:11px;color:var(--text3);display:none;margin-left:2px;"></span>
      </div>

      <div class="pipeline-grid">
        <div class="steps-main" id="steps-hotfix"></div>
        <div style="display:flex;flex-direction:column;gap:12px;min-width:0">
          <div class="sidebar-card">
            <div class="sidebar-title">Progresso</div>
            <div class="docker-info" id="hotfix-pipeline-progress"></div>
          </div>
          <div class="sidebar-card">
            <div class="sidebar-title">Checklist manual (não automatizável)</div>
            <div class="docker-info" id="hotfix-checklist"></div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- ── ESTEIRA ─────────────────────────────────────────────────────────── -->
  <div id="page-esteira" class="page">
    <div class="page-content">
      <div class="kanban-toolbar">
        <button class="btn btn-primary" onclick="openTaskModal(null,'planejadas')">+ Nova atividade</button>
        <button class="btn btn-ghost" id="btn-jira-sync" onclick="syncJira()">⟳ Importar do Jira</button>
        <select id="filter-sprint" onchange="renderKanban()">
          <option value="">Todas as sprints</option>
        </select>
        <select id="filter-person" onchange="renderKanban()">
          <option value="">Todos</option>
        </select>
        <select id="filter-type" onchange="renderKanban()">
          <option value="">Todos os tipos</option>
          <option value="feat">Feature</option>
          <option value="fix">Fix</option>
          <option value="chore">Chore</option>
          <option value="other">Outro</option>
        </select>
        <input id="filter-search" placeholder="Buscar…" oninput="renderKanban()" style="min-width:140px">
      </div>
      <div class="kanban-board" id="kanban-board"></div>
    </div>
  </div>

  <!-- ── TIME ───────────────────────────────────────────────────────────────── -->
  <div id="page-time" class="page">
    <div class="page-content">
      <div class="time-header">
        <select class="time-filter" id="time-sprint-filter" onchange="renderTime()">
          <option value="">Todas as sprints</option>
        </select>
        <button class="btn btn-primary" onclick="openTaskModal(null,'planejadas')">+ Nova atividade</button>
      </div>
      <div class="member-grid" id="member-grid"></div>
    </div>
  </div>

  <!-- ── KPIs ──────────────────────────────────────────────────────────────── -->
  <div id="page-kpis" class="page">
    <div class="page-content">

      <div class="kpi-page-hero">
        <div class="kpi-hero-left">
          <div class="kpi-hero-brand">NEOS</div>
          <h2 class="kpi-hero-title">KPIs Frontend</h2>
          <div class="kpi-hero-sub">Acompanhamento do desempenho do time de Front-End</div>
        </div>
        <div class="kpi-hero-right">
          <div class="kpi-release-tabs">
            <div class="kpi-release-tab kpi-rtab-base">
              <span class="kpi-rtab-release" id="kpi-rtab-base-name">—</span>
              <span class="kpi-rtab-role">Baseline</span>
            </div>
            <div class="kpi-release-tab kpi-rtab-cur">
              <span class="kpi-rtab-release" id="kpi-rtab-cur-name">—</span>
              <span class="kpi-rtab-role">Atual</span>
            </div>
            <div class="kpi-release-tab kpi-rtab-evo">
              <span class="kpi-rtab-role">EVOLUÇÃO</span>
              <span class="kpi-rtab-narrative" id="kpi-rtab-narrative">—</span>
            </div>
          </div>
          <div class="kpi-cmp-selects-row">
            <div class="kpi-cmp-select-group">
              <label>Baseline</label>
              <select class="time-filter" id="kpi-cmp-baseline" onchange="renderReleaseComparison()"></select>
            </div>
            <span class="kpi-cmp-arrow">→</span>
            <div class="kpi-cmp-select-group">
              <label>Atual</label>
              <select class="time-filter" id="kpi-cmp-current" onchange="renderReleaseComparison()"></select>
            </div>
          </div>
        </div>
      </div>

      <div class="kpi-cmp-cards" id="kpi-cmp-cards"></div>

      <div class="kpi-two-col">
        <div class="kpi-section">
          <div class="kpi-section-title">Comparativo entre releases</div>
          <div class="kpi-cmp-charts" id="kpi-cmp-charts"></div>
        </div>
        <div class="kpi-section">
          <div class="kpi-section-title">Leitura executiva</div>
          <div id="kpi-cmp-insights"></div>
        </div>
      </div>

      <div class="kpi-section" style="margin-bottom:20px;">
        <div class="kpi-section-title">Resumo das releases</div>
        <div id="kpi-cmp-summary"></div>
      </div>

      <div class="kpi-header">
        <select class="time-filter" id="kpi-sprint-filter" onchange="renderKPIs()">
          <option value="">Sprint atual</option>
        </select>
        <span style="font-size:12px;color:var(--text3)" id="kpi-sprint-label"></span>
      </div>

      <div class="kpi-metrics">
        <div class="kpi-metric">
          <div class="kpi-metric-num" id="kpi-total">—</div>
          <div class="kpi-metric-lbl">Total na Sprint</div>
        </div>
        <div class="kpi-metric">
          <div class="kpi-metric-num" id="kpi-done" style="color:var(--success)">—</div>
          <div class="kpi-metric-lbl">Entregues</div>
        </div>
        <div class="kpi-metric">
          <div class="kpi-metric-num" id="kpi-rate">—</div>
          <div class="kpi-metric-lbl">Taxa de entrega</div>
        </div>
        <div class="kpi-metric">
          <div class="kpi-metric-num" id="kpi-carryover" style="color:var(--warn)">—</div>
          <div class="kpi-metric-lbl">Carryover</div>
        </div>
        <div class="kpi-metric">
          <div class="kpi-metric-num" id="kpi-hotfixes" style="color:var(--danger)">—</div>
          <div class="kpi-metric-lbl">Fixes / Hotfixes</div>
        </div>
        <div class="kpi-metric">
          <div class="kpi-metric-num" id="kpi-overdue" style="color:var(--danger)">—</div>
          <div class="kpi-metric-lbl">Atrasadas</div>
        </div>
      </div>

      <div class="kpi-section">
        <div class="kpi-section-title">Performance por Dev</div>
        <div class="kpi-devs-grid" id="kpi-devs"></div>
      </div>

      <div class="kpi-two-col">
        <div class="kpi-section">
          <div class="kpi-section-title">Histórico de Sprints</div>
          <div id="kpi-history"></div>
        </div>
        <div class="kpi-section">
          <div class="kpi-section-title">Distribuição por Tipo</div>
          <div id="kpi-types"></div>
        </div>
      </div>

    </div>
  </div>

  <!-- ── FORMALIZAR ────────────────────────────────────────────────────── -->
  <div id="page-formalizar" class="page">
    <div class="page-content">

      <div class="fz-page-header">
        <div>
          <h2 class="fz-page-title">Formalizar no Slack</h2>
        </div>
        <button class="fz-send-all-btn" onclick="fzSendAll()">▶ Enviar todos pendentes</button>
      </div>

      <!-- Reply spec editável -->
      <div class="fz-reply-box">
        <div class="fz-reply-label">Resposta enviada em cada formalização</div>
        <textarea class="fz-reply-ta" id="fz-reply-spec" rows="4"
          placeholder="CRM:&#10;Release 57.0&#10;Frontend:&#10;v2.55.0_zcrm_api_2.55.0"
          oninput="fzOnReplySpecChange()"></textarea>
        <div class="fz-reply-hint" id="fz-reply-hint"></div>
      </div>

      <!-- Form: adicionar item único -->
      <div class="fz-add-box">
        <div class="fz-add-box-title">Adicionar atividade</div>
        <div class="fz-row">
          <div class="fz-field fz-field-sm">
            <label>Ticket</label>
            <input class="form-control" id="fz-ticket" placeholder="NEOSDEV-6519"
              oninput="fzOnTicketInput()">
          </div>
          <div class="fz-field fz-field-grow">
            <label>Branch</label>
            <div class="fz-branch-row">
              <select class="form-control" id="fz-branch-sel" onchange="fzOnBranchSel()">
                <option value="">— busque ou digite abaixo —</option>
              </select>
              <button class="btn btn-secondary" onclick="fzSearchBranches()" id="fz-search-btn" style="white-space:nowrap">Buscar no git</button>
            </div>
          </div>
          <div class="fz-field fz-field-md">
            <label>Branch manual (se não encontrar)</label>
            <input class="form-control" id="fz-branch-manual" placeholder="hotfix/NEOSDEV-6519-..."
              oninput="fzOnManualBranch()">
          </div>
        </div>
        <div class="fz-row">
          <div class="fz-field fz-field-grow">
            <label>Conteúdo</label>
            <input class="form-control" id="fz-conteudo" placeholder="#NEOSDEV-6519# - Módulo - Descrição da correção">
          </div>
          <div class="fz-field fz-field-md">
            <label>Subtask (opcional)</label>
            <input class="form-control" id="fz-subtask" placeholder="NEOSDEV-6521">
          </div>
        </div>
        <div class="fz-add-footer" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <button class="btn btn-primary" onclick="fzAddItem()">+ Adicionar à lista</button>
          <button class="fz-bulk-toggle" onclick="fzToggleBulk()">+ Adicionar múltiplos tickets de uma vez</button>
        </div>
        <!-- Bulk add (oculto por padrão) -->
        <div class="fz-bulk-box" id="fz-bulk-box" style="display:none;margin-top:14px">
          <div class="fz-reply-label">Tickets (um por linha)</div>
          <textarea class="fz-bulk-ta" id="fz-bulk-tickets" rows="5"
            placeholder="NEOSDEV-6519&#10;NEOSDEV-6540&#10;NEOSDEV-6548&#10;NEOSDEV-6479"></textarea>
          <div class="fz-bulk-hint">A branch será buscada automaticamente no git. Se não encontrar, você poderá editar o item depois.</div>
          <button class="btn btn-primary" onclick="fzBulkAdd()" id="fz-bulk-btn">Buscar e adicionar todas</button>
        </div>
      </div>

      <!-- Lista de itens -->
      <div class="fz-section-hdr">Atividades para formalizar</div>
      <div id="fz-list"></div>

    </div>
  </div>

  <!-- ── HISTÓRICO ──────────────────────────────────────────────────────── -->
  <div id="page-historico" class="page">
    <div class="page-content">
      <div class="fz-page-header">
        <div>
          <h2 class="fz-page-title">Histórico de Pipelines</h2>
          <p style="font-size:13px;color:var(--text3);margin-top:3px">Todas as execuções de release e hotfix com métricas DORA.</p>
        </div>
      </div>

      <div class="dora-row">
        <div class="dora-card dc-df">
          <div class="dora-label">Deploy Frequency</div>
          <div class="dora-val" id="dora-df">—</div>
          <div class="dora-unit">deploys / semana (30d)</div>
          <div class="dora-badge" id="dora-df-badge"></div>
        </div>
        <div class="dora-card dc-lt">
          <div class="dora-label">Lead Time</div>
          <div class="dora-val" id="dora-lt">—</div>
          <div class="dora-unit">minutos — média release</div>
          <div class="dora-badge" id="dora-lt-badge"></div>
        </div>
        <div class="dora-card dc-cfr">
          <div class="dora-label">Change Failure Rate</div>
          <div class="dora-val" id="dora-cfr">—</div>
          <div class="dora-unit">% das execuções com falha</div>
          <div class="dora-badge" id="dora-cfr-badge"></div>
        </div>
        <div class="dora-card dc-mttr">
          <div class="dora-label">MTTR</div>
          <div class="dora-val" id="dora-mttr">—</div>
          <div class="dora-unit">minutos — média hotfix</div>
          <div class="dora-badge" id="dora-mttr-badge"></div>
        </div>
      </div>

      <div class="card">
        <div class="card-header">
          <span class="card-header-title">Execuções recentes</span>
          <button class="card-header-action" onclick="loadHistory()">↻ Atualizar</button>
        </div>
        <div id="hist-list"><div class="hist-empty">Carregando…</div></div>
      </div>
    </div>
  </div>

</div><!-- /main -->

<!-- ══ MODALS ════════════════════════════════════════════════════════════ -->

<!-- release wizard modal -->
<div class="modal-overlay" id="wizard-modal">
  <div class="modal modal-wide">
    <div class="modal-title">▶ Iniciar Release</div>
    <p style="font-size:12px;color:var(--text2);margin-bottom:18px">Revise os parâmetros detectados automaticamente. Edite o que precisar antes de confirmar.</p>

    <div class="form-row-2">
      <div class="form-row">
        <label class="form-label">Branch da release atual</label>
        <input class="form-control" id="wz-cur-branch" placeholder="release/v2.53.0_zcrm_api_2.53.0_RC">
      </div>
      <div class="form-row">
        <label class="form-label">Versão frontend</label>
        <input class="form-control" id="wz-version" placeholder="v2.53.0_zcrm_api_2.53.0">
      </div>
    </div>
    <div class="form-row-2">
      <div class="form-row">
        <label class="form-label">CRM Version atual</label>
        <input class="form-control" id="wz-crm" placeholder="55.0">
      </div>
      <div class="form-row">
        <label class="form-label">Branch da próxima release</label>
        <input class="form-control" id="wz-next-branch" placeholder="release/v2.54.0_zcrm_api_2.54.0_RC">
      </div>
    </div>
    <div class="form-row">
      <label class="form-label">CRM Version da próxima release</label>
      <input class="form-control" id="wz-next-crm" placeholder="56.0">
    </div>
    <div class="form-row">
      <label class="form-label">
        Branches para merge
        <span style="color:var(--text3);font-weight:400;font-size:11px"> — separadas por vírgula (deixe vazio para buscar via Slack)</span>
      </label>
      <textarea class="form-control" id="wz-branches" rows="3" placeholder="feature/ZCRM-1234, feature/ZCRM-5678, hotfix/login-fix"></textarea>
    </div>

    <div class="form-row">
      <label class="form-label">Iniciar a partir da etapa</label>
      <select class="form-control" id="wz-from-step" onchange="onWzStepChange()">
        <option value="1">01 — Do início (completo)</option>
        <option value="2">02 — Checkout release atual</option>
        <option value="3">03 — Coletar branches</option>
        <option value="4">04 — Merge das branches</option>
        <option value="5">05 — Atualizar CHANGELOG</option>
        <option value="6">06 — Validar package.json</option>
        <option value="7">07 — ng build --prod</option>
        <option value="8">08 — Criar tag Git</option>
        <option value="9">09 — Merge release → develop</option>
        <option value="10">10 — Merge develop → master</option>
        <option value="11">11 — Docker Build</option>
        <option value="12">12 — Docker Push</option>
        <option value="13">13 — Criar próxima release</option>
        <option value="14">14 — Atualizar package.json</option>
        <option value="15">15 — Gerar imagem RC</option>
        <option value="16">16 — Criar branch dev</option>
        <option value="17">17 — Anúncio no Slack</option>
        <option value="18">18 — Relatório final</option>
      </select>
    </div>

    <div id="wz-resume-notice" style="display:none;background:rgba(217,119,6,.15);border:1px solid var(--warn);border-radius:var(--radius);padding:10px 14px;font-size:12px;color:var(--warn);line-height:1.5;margin-top:2px">
      ⚠ As etapas anteriores serão puladas. Os campos acima (branch, versão, CRM) ainda são usados como contexto para as etapas selecionadas.
    </div>

    <div id="wz-actions-box" style="background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);padding:12px 14px;margin-top:4px;font-size:12px;color:var(--text2);line-height:1.6">
      <strong style="color:var(--text)">Ações que serão executadas:</strong><br>
      Fechar release atual · Merge das branches · Atualizar CHANGELOG · Criar tag Git ·
      Merge para develop e master · Build e push Docker · Criar próxima release
    </div>

    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeWizard()">Cancelar</button>
      <button class="btn btn-primary" id="wz-confirm-btn" onclick="confirmWizard()">▶ Confirmar e Iniciar</button>
    </div>
  </div>
</div>

<!-- config modal -->
<div class="modal-overlay" id="cfg-modal">
  <div class="modal">
    <div class="modal-title">Configurar Release</div>
    <div class="form-row-2">
      <div class="form-row">
        <label class="form-label">Branch atual</label>
        <input class="form-control" id="m-cur-branch" placeholder="release/v2.53.0_zcrm_api_2.53.0_RC">
      </div>
      <div class="form-row">
        <label class="form-label">Versão atual</label>
        <input class="form-control" id="m-version" placeholder="v2.53.0_zcrm_api_2.53.0">
      </div>
    </div>
    <div class="form-row-2">
      <div class="form-row">
        <label class="form-label">CRM Version</label>
        <input class="form-control" id="m-crm" placeholder="55.0">
      </div>
      <div class="form-row">
        <label class="form-label">Sprint atual</label>
        <input class="form-control" id="m-sprint" placeholder="Sprint 55">
      </div>
    </div>
    <div class="form-row-2">
      <div class="form-row">
        <label class="form-label">Próximo branch</label>
        <input class="form-control" id="m-next-branch" placeholder="release/v2.54.0…_RC">
      </div>
      <div class="form-row">
        <label class="form-label">Próxima versão</label>
        <input class="form-control" id="m-next-version" placeholder="v2.54.0_zcrm_api_2.54.0">
      </div>
    </div>
    <div class="form-row-2">
      <div class="form-row">
        <label class="form-label">Próximo CRM</label>
        <input class="form-control" id="m-next-crm" placeholder="56.0">
      </div>
      <div class="form-row">
        <label class="form-label">Docker registry</label>
        <input class="form-control" id="m-registry" placeholder="zanthusinovacao1/zcrm_front">
      </div>
    </div>
    <div class="form-row-2">
      <div class="form-row">
        <label class="form-label">Nome do time</label>
        <input class="form-control" id="m-team" placeholder="Front-end ZCRM">
      </div>
      <div class="form-row">
        <label class="form-label">Slack User ID (DM)</label>
        <input class="form-control" id="m-slack-uid" placeholder="U01ABC123"
          title="ID do membro no Slack — clique no seu nome → ⋯ → Copiar ID do membro">
      </div>
    </div>
    <div style="font-size:11px;color:var(--text3);margin:-8px 0 12px;">
      💬 Com o Slack User ID configurado, o bot vai te mandar DM quando a pipeline falhar, ter conflito, terminar — e ao formalizar.
    </div>
    <div class="modal-footer">
      <button class="btn btn-ghost" onclick="closeCfgModal()">Cancelar</button>
      <button class="btn btn-primary" onclick="saveCfgModal()">Salvar</button>
    </div>
  </div>
</div>

<!-- task card detail -->
<div class="cd-overlay" id="task-modal">
  <div class="cd-card">

    <!-- header -->
    <div class="cd-header">
      <select class="cd-status-select" id="t-status"></select>
      <div class="cd-header-sep"></div>
      <button class="cd-close" onclick="closeTaskModal()">✕</button>
    </div>

    <!-- body -->
    <div class="cd-body">

      <!-- main -->
      <div class="cd-main">
        <input type="hidden" id="task-id">

        <textarea class="cd-title-input" id="t-title" rows="2" placeholder="Título da atividade…"></textarea>

        <!-- labels -->
        <div>
          <div class="cd-section-lbl">Etiquetas</div>
          <div class="cd-chips-row">
            <select class="form-control" id="t-type" style="width:auto;font-size:12px;padding:4px 8px;">
              <option value="feat">Feature</option>
              <option value="fix">Fix</option>
              <option value="chore">Chore</option>
              <option value="other">Outro</option>
            </select>
            <select class="form-control" id="t-prio" style="width:auto;font-size:12px;padding:4px 8px;">
              <option value="high">🔴 Alta</option>
              <option value="medium">🟡 Média</option>
              <option value="low">🟢 Baixa</option>
            </select>
          </div>
        </div>

        <!-- description / notes -->
        <div style="flex:1">
          <div class="cd-section-lbl">Descrição / Observações</div>
          <textarea class="cd-notes" id="t-notes" placeholder="Adicione contexto, links, bloqueios…"></textarea>
        </div>
      </div>

      <!-- sidebar -->
      <div class="cd-sidebar">

        <div class="cd-field">
          <span class="cd-field-lbl">Ticket JIRA</span>
          <div class="cd-jira-link">
            <input class="cd-field-input" id="t-jira" placeholder="ZCRM-1234" oninput="updateJiraLink()" onblur="loadWorklogs()">
          </div>
          <a id="t-jira-link" href="#" target="_blank" style="font-size:11px;color:var(--info);display:none;margin-top:3px;">Abrir no JIRA ↗</a>
        </div>

        <div class="cd-field" id="cd-worklog-section" style="border-top:1px solid var(--border);padding-top:12px;">
          <span class="cd-field-lbl">Horas apontadas (Jira)</span>
          <div id="cd-worklog-total" style="font-size:22px;font-weight:700;color:var(--text);margin:4px 0 10px;">—</div>

          <input class="cd-field-input" id="cd-worklog-time" placeholder="ex: 2h, 1d, 30m" style="margin-bottom:6px;">
          <input class="cd-field-input" id="cd-worklog-comment" placeholder="Comentário (opcional)" style="margin-bottom:6px;">
          <button class="btn btn-primary" id="cd-worklog-btn" onclick="logWorklog()" style="width:100%;font-size:12px;padding:7px;">+ Lançar horas</button>
          <div id="cd-worklog-status" style="display:none;font-size:11px;margin-top:6px;padding:6px 8px;border-radius:5px;"></div>

          <div id="cd-worklog-list" style="margin-top:10px;max-height:150px;overflow-y:auto;font-size:11px;color:var(--text3);"></div>
        </div>

        <div class="cd-field">
          <span class="cd-field-lbl">Branch</span>
          <input class="cd-field-input" id="t-branch" placeholder="feature/ZCRM-1234">
        </div>

        <div class="cd-field">
          <span class="cd-field-lbl">Sprint</span>
          <input class="cd-field-input" id="t-sprint" placeholder="Sprint 55">
        </div>

        <div class="cd-field">
          <span class="cd-field-lbl">Responsável</span>
          <input class="cd-field-input" id="t-person" placeholder="Nome do dev">
        </div>

        <div class="cd-field">
          <span class="cd-field-lbl">Data de entrada</span>
          <input class="cd-field-input" type="date" id="t-date">
        </div>

        <div class="cd-field">
          <span class="cd-field-lbl">Data prevista</span>
          <input class="cd-field-input" type="date" id="t-due">
        </div>

      </div>
    </div>

    <!-- footer -->
    <div class="cd-footer">
      <button class="cd-del btn" id="task-del-btn" onclick="deleteTask()" style="display:none">Excluir</button>
      <button class="btn btn-ghost" onclick="closeTaskModal()">Cancelar</button>
      <button class="btn btn-primary" onclick="saveTask()">Salvar</button>
    </div>
  </div>
</div>

<!-- Conflict resolution modal -->
<div class="modal-overlay" id="rollback-modal">
  <div class="modal" style="max-width:580px;width:100%">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:16px">
      <span style="font-size:22px;line-height:1">↩</span>
      <span style="font-size:16px;font-weight:700;color:var(--text)">Reverter pipeline local</span>
    </div>
    <p style="color:var(--text2);font-size:13px;line-height:1.55;margin:0 0 14px">
      Isso desfaz todos os commits locais feitos pela pipeline (merges + changelog) usando
      <code style="background:var(--surface2);padding:1px 5px;border-radius:3px;font-size:11px">git reset --hard</code>.
      Só funciona se o push <strong>ainda não aconteceu</strong>.
    </p>
    <div id="rollback-info-box" style="margin-bottom:16px">
      <div style="color:var(--text3);font-size:12px">Carregando informações…</div>
    </div>
    <div id="rollback-commits-box" style="display:none">
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin-bottom:8px">Commits que serão desfeitos</div>
      <div id="rollback-commits-list" style="
        background:var(--surface2);border:1px solid rgba(239,68,68,.2);border-radius:8px;
        padding:10px 14px;max-height:220px;overflow-y:auto;
        font-family:monospace;font-size:11px;display:flex;flex-direction:column;gap:5px;
      "></div>
      <div style="font-size:11px;color:var(--text3);margin-top:8px">
        HEAD será restaurado para: <code id="rollback-target-sha" style="background:var(--surface2);padding:1px 6px;border-radius:3px"></code>
      </div>
    </div>
    <div style="display:flex;gap:10px;justify-content:flex-end;margin-top:20px">
      <button class="btn" onclick="closeRollbackModal()" style="background:var(--surface2);color:var(--text2);border:1px solid var(--border)">Cancelar</button>
      <button class="btn btn-danger" id="rollback-confirm-btn" onclick="confirmRollback()" disabled>↩ Reverter commits locais</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="step-fail-modal">
  <div class="modal" style="max-width:520px;width:100%">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      <span style="font-size:22px;line-height:1">❌</span>
      <span style="font-size:16px;font-weight:700;color:var(--text)">Etapa <span id="sfm-step-num">—</span> falhou</span>
    </div>
    <div style="background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:12px;font-family:monospace;color:var(--text2);word-break:break-all" id="sfm-error"></div>
    <div id="sfm-hint-box" style="display:none;background:rgba(6,182,212,.06);border:1px solid rgba(6,182,212,.2);border-radius:8px;padding:12px 16px;margin-bottom:16px"></div>
    <p style="color:var(--text2);font-size:13px;line-height:1.55;margin:0 0 14px">O que deseja fazer?</p>
    <div style="display:flex;flex-direction:column;gap:10px">
      <button class="btn" id="sfm-skip-btn" onclick="stepFailDecision('skip')" style="background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);color:var(--warn,#d97706);font-size:13px;text-align:left;padding:12px 16px">
        <strong>⏭ Pular esta etapa e continuar</strong><br>
        <span style="font-size:11px;opacity:.8">A etapa fica marcada como pulada. As próximas etapas serão executadas normalmente.</span>
      </button>
      <button class="btn" id="sfm-abort-btn" onclick="stepFailDecision('abort')" style="background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.2);color:var(--danger);font-size:13px;text-align:left;padding:12px 16px">
        <strong>⛔ Interromper pipeline</strong><br>
        <span style="font-size:11px;opacity:.8">Commits locais não serão desfeitos automaticamente nesta etapa.</span>
      </button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="conflict-modal">
  <div class="modal" style="max-width:560px;width:100%">

    <div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">
      <span style="font-size:22px;line-height:1">⚠️</span>
      <span style="font-size:16px;font-weight:700;color:var(--text)">Conflitos de merge detectados</span>
    </div>

    <div id="conflict-step-badge" style="
      display:inline-flex;align-items:center;gap:6px;
      background:rgba(217,119,6,.15);border:1px solid rgba(217,119,6,.35);
      color:var(--warn,#d97706);border-radius:6px;padding:4px 10px;
      font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;
      margin-bottom:16px;
    ">⏸ Pipeline pausada nesta etapa</div>

    <p style="color:var(--text2);font-size:13px;line-height:1.55;margin:0 0 16px">
      As branches abaixo causaram conflito no merge. A pipeline ficou pausada e aguarda você corrigir.
    </p>

    <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.07em;color:var(--text3);margin-bottom:8px">Branches com conflito</div>
    <div id="conflict-branch-list" style="
      background:var(--surface2);border:1px solid rgba(239,68,68,.25);border-radius:8px;
      padding:12px 16px;margin-bottom:20px;
      display:flex;flex-direction:column;gap:8px;
    "></div>

    <div style="
      background:rgba(6,182,212,.07);border:1px solid rgba(6,182,212,.2);
      border-radius:8px;padding:12px 16px;margin-bottom:20px;
      font-size:12.5px;color:var(--text2);line-height:1.6;
    ">
      <strong style="color:var(--text);display:block;margin-bottom:4px">Como resolver:</strong>
      1. Abra o terminal no repositório <code style="background:var(--surface);padding:1px 5px;border-radius:3px;font-size:11px">zcrm_front</code><br>
      2. Para cada branch acima: resolva os conflitos nos arquivos, depois rode:<br>
      <code style="background:var(--surface);padding:3px 8px;border-radius:4px;font-size:11px;display:inline-block;margin-top:4px">git add . &amp;&amp; git commit --no-edit</code><br>
      3. Quando todas estiverem resolvidas, clique em <strong style="color:var(--text)">Confirmar resolução</strong> abaixo.
    </div>

    <div style="display:flex;gap:10px;justify-content:flex-end">
      <button class="btn" onclick="closeConflictModal()" style="background:var(--surface2);color:var(--text2);border:1px solid var(--border)">
        Fechar (sem resolver)
      </button>
      <button class="btn btn-primary" id="conflict-confirm-btn" onclick="confirmConflictResolved()">
        ✓ Confirmar resolução e continuar
      </button>
    </div>

  </div>
</div>

<div class="modal-overlay" id="jira-modal">
  <div class="modal" style="width:620px;">
    <div class="modal-title">⟳ Importar do Jira</div>
    <div class="form-row">
      <label class="form-label">
        JQL de busca (equivalente ao filtro Categorias = neos-frontend + Versões Corrigidas da release atual)
      </label>
      <textarea class="form-control" id="jira-modal-jql" rows="3" style="font-family:monospace;font-size:12px;"></textarea>
    </div>
    <div id="jira-modal-status" style="display:none;font-size:12.5px;padding:10px 12px;border-radius:6px;margin-bottom:4px;"></div>

    <div id="jira-modal-conflicts" style="display:none;margin-bottom:14px;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
        <span style="font-size:12px;color:var(--text2);" id="jira-conflicts-count"></span>
        <span style="display:flex;gap:10px;">
          <a href="#" style="font-size:11.5px;color:var(--accent);" onclick="event.preventDefault();_toggleAllJiraConflicts(true)">Marcar todas</a>
          <a href="#" style="font-size:11.5px;color:var(--text3);" onclick="event.preventDefault();_toggleAllJiraConflicts(false)">Desmarcar todas</a>
        </span>
      </div>
      <div id="jira-conflicts-list" style="max-height:280px;overflow-y:auto;border:1px solid var(--border);border-radius:8px;"></div>
    </div>

    <div class="modal-footer">
      <button class="btn btn-ghost" id="jira-modal-cancel" onclick="closeJiraModal()">Cancelar</button>
      <button class="btn btn-primary" id="jira-modal-confirm" onclick="previewJiraSync()">⟳ Importar</button>
      <button class="btn btn-primary" id="jira-modal-apply" style="display:none;" onclick="applyJiraSync()">✓ Aplicar sincronização</button>
    </div>
  </div>
</div>

<script>
// ── state ────────────────────────────────────────────────────────────────────
let cfg   = {};
let tasks = [];
let es    = null;
let stepLogs       = {};   // {stepNum: [lines]}
let _currentStep   = 0;
let _stepStartTs   = {};   // {stepNum: timestamp}
let _lastExitCode  = null;

const COLS = [
  { id:'importantes',  name:'IMPORTANTES',                        color:'#EF4444' },
  { id:'planejadas',   name:'Tarefas Planejadas',                 color:'#94A3B8' },
  { id:'solicitacoes', name:'Solicitações',                       color:'#A855F7' },
  { id:'andamento',    name:'Em Andamento',                       color:'#3B82F6' },
  { id:'aguardando',   name:'Aguardando Autorização para subir',  color:'#F59E0B' },
  { id:'validacao',    name:'Em validação',                       color:'#F97316' },
  { id:'formalizar',   name:'Para Formalizar',                    color:'#14B8A6' },
  { id:'formalizadas', name:'Formalizadas',                       color:'#22C55E' },
  { id:'etapas',       name:'Etapas Release',                     color:'#6366F1' },
  { id:'concluidas',   name:'Concluídas',                         color:'#16A34A' },
];

const COL_COLORS = {
  importantes:'#EF4444', planejadas:'#94A3B8', solicitacoes:'#A855F7',
  andamento:'#3B82F6', aguardando:'#F59E0B', validacao:'#F97316',
  formalizar:'#14B8A6', formalizadas:'#22C55E', etapas:'#6366F1', concluidas:'#16A34A',
};

const DEFAULT_STEPS = [
  {id:1,  name:'Validação do ambiente',    dur:'—', status:'pending'},
  {id:2,  name:'Checkout release atual',   dur:'—', status:'pending'},
  {id:3,  name:'Coletar branches',         dur:'—', status:'pending'},
  {id:4,  name:'Merge das branches',       dur:'—', status:'pending'},
  {id:5,  name:'Atualizar CHANGELOG',      dur:'—', status:'pending'},
  {id:6,  name:'Validar package.json',     dur:'—', status:'pending'},
  {id:7,  name:'ng build --prod',          dur:'—', status:'pending'},
  {id:8,  name:'Criar tag Git',            dur:'—', status:'pending'},
  {id:9,  name:'Merge release → develop',  dur:'—', status:'pending'},
  {id:10, name:'Merge develop → master',   dur:'—', status:'pending'},
  {id:11, name:'Docker Build',             dur:'—', status:'pending'},
  {id:12, name:'Docker Push',              dur:'—', status:'pending'},
  {id:13, name:'Criar próxima release',    dur:'—', status:'pending'},
  {id:14, name:'Atualizar package.json',   dur:'—', status:'pending'},
  {id:15, name:'Gerar imagem RC',          dur:'—', status:'pending'},
  {id:16, name:'Criar branch dev',         dur:'—', status:'pending'},
  {id:17, name:'Anúncio no Slack',         dur:'—', status:'pending'},
  {id:18, name:'Relatório final',          dur:'—', status:'pending'},
];

let pipelineSteps = JSON.parse(JSON.stringify(DEFAULT_STEPS));

const DEFAULT_HOTFIX_STEPS = [
  {id:1,  name:'Validação do ambiente',           dur:'—', status:'pending'},
  {id:2,  name:'Checkout da release alvo',        dur:'—', status:'pending'},
  {id:3,  name:'Merge da branch de hotfix',       dur:'—', status:'pending'},
  {id:4,  name:'Atualizar CHANGELOG',             dur:'—', status:'pending'},
  {id:5,  name:'Atualizar package.json + commit', dur:'—', status:'pending'},
  {id:6,  name:'ng build --prod',                 dur:'—', status:'pending'},
  {id:7,  name:'Criar tag do hotfix',              dur:'—', status:'pending'},
  {id:8,  name:'Propagar para a release atual',   dur:'—', status:'pending'},
  {id:9,  name:'Docker build + push',             dur:'—', status:'pending'},
  {id:10, name:'Relatório final',                 dur:'—', status:'pending'},
];

let hotfixStepsState   = JSON.parse(JSON.stringify(DEFAULT_HOTFIX_STEPS));
let hotfixStepLogs     = {};   // {stepNum: [lines]}
let _hotfixCurrentStep = 0;
let _hotfixStepStartTs = {};
let activePipeline     = 'release';  // 'release' | 'hotfix' — roteia as mensagens SSE

const STEP_ICON = {
  success:{cls:'si-success',icon:'✓'},
  failed: {cls:'si-failed', icon:'✕'},
  running:{cls:'si-running',icon:'◉'},
  skipped:{cls:'si-skipped',icon:'—'},
  pending:{cls:'si-pending',icon:'○'},
};

// ── theme ─────────────────────────────────────────────────────────────────────
let theme = localStorage.getItem('zcrm_theme') || 'system';
function applyTheme() {
  const r = document.documentElement;
  if (theme === 'dark')  r.setAttribute('data-theme','dark');
  else if (theme === 'light') r.setAttribute('data-theme','light');
  else r.removeAttribute('data-theme');
}
function toggleTheme() {
  theme = theme === 'system' ? 'dark' : theme === 'dark' ? 'light' : 'system';
  localStorage.setItem('zcrm_theme', theme);
  applyTheme();
}
applyTheme();

// ── nav ───────────────────────────────────────────────────────────────────────
const PAGE_TITLES = {
  overview:'Visão Geral', pipeline:'Pipeline', hotfix:'Hotfix',
  esteira:'Esteira do Time', time:'Time', kpis:'KPIs do Time',
  formalizar:'Formalizar no Slack', historico:'Histórico de Pipelines',
};

function goto(id, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.sb-item').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + id).classList.add('active');
  if (el) el.classList.add('active');
  document.getElementById('topbar-title').textContent = PAGE_TITLES[id] || id;
  if (id === 'formalizar') renderFormalizar();
  if (id === 'historico')  loadHistory();
}

function gotoByName(id) {
  const items = document.querySelectorAll('.sb-item');
  const names = ['overview','pipeline','hotfix','esteira','time','kpis','formalizar','historico'];
  const i = names.indexOf(id);
  goto(id, i >= 0 ? items[i] : null);
}

// ── init ───────────────────────────────────────────────────────────────────────
async function init() {
  // date
  document.getElementById('topbar-date').textContent = new Date().toLocaleDateString('pt-BR',{weekday:'long',day:'numeric',month:'long',year:'numeric'});

  await loadConfig();
  await loadTasks();
  await loadReport();
  loadHistory();   // non-blocking: populates overview widget + history page

  renderAll();
  renderHotfixChecklist();

  const r = await fetch('/api/release/status');
  const s = await r.json();
  if (s.running) {
    activePipeline = s.pipeline || 'release';
    if (activePipeline === 'hotfix') {
      setHotfixRunningUI(true, s.paused || false);
      if (s.step) { _hotfixCurrentStep = s.step; _updateHotfixStepCard(s.step, s.paused ? 'paused' : 'running'); }
    } else {
      setRunningUI(true, s.paused || false);
      if (s.step) { _currentStep = s.step; _updateStepCard(s.step, s.paused ? 'paused' : 'running'); }
    }
    connectStream();
  }
}

async function loadConfig() {
  const r = await fetch('/api/config');
  cfg = await r.json();
}

async function loadTasks() {
  const r = await fetch('/api/tasks');
  tasks = await r.json();
}

// ── history & DORA ────────────────────────────────────────────────────────────
let _history = [];

async function loadHistory() {
  try {
    const r = await fetch('/api/history');
    _history = await r.json();
  } catch(e) { _history = []; }
  renderHistory();
}

function _fmtDur(s) {
  if (!s && s !== 0) return '—';
  const m = Math.floor(s / 60), sec = s % 60;
  return m > 0 ? `${m}m ${sec}s` : `${sec}s`;
}

function _doraLevel(metric, value) {
  if (metric === 'df')   return value >= 7 ? {l:'elite',n:'Elite'} : value >= 1 ? {l:'high',n:'Alto'} : value >= 0.25 ? {l:'medium',n:'Médio'} : {l:'low',n:'Baixo'};
  if (metric === 'lt')   return value <= 60 ? {l:'elite',n:'Elite'} : value <= 180 ? {l:'high',n:'Alto'} : value <= 720 ? {l:'medium',n:'Médio'} : {l:'low',n:'Baixo'};
  if (metric === 'cfr')  return value <= 5 ? {l:'elite',n:'Elite'} : value <= 10 ? {l:'high',n:'Alto'} : value <= 30 ? {l:'medium',n:'Médio'} : {l:'low',n:'Baixo'};
  if (metric === 'mttr') return value <= 60 ? {l:'elite',n:'Elite'} : value <= 240 ? {l:'high',n:'Alto'} : value <= 1440 ? {l:'medium',n:'Médio'} : {l:'low',n:'Baixo'};
  return {l:'medium',n:'—'};
}

function _setBadge(id, metric, value) {
  const el = document.getElementById(id);
  if (!el) return;
  if (!value && value !== 0) { el.textContent = ''; el.className = 'dora-badge'; return; }
  const d = _doraLevel(metric, value);
  el.textContent = d.n;
  el.className = 'dora-badge ' + d.l;
}

function renderHistory() {
  const histEl = document.getElementById('hist-list');
  const thirtyAgo = Date.now() - 30 * 24 * 3600 * 1000;
  const recent = _history.filter(e => new Date(e.started_at).getTime() >= thirtyAgo);

  if (!_history.length) {
    histEl.innerHTML = '<div class="hist-empty">Nenhuma execução registrada ainda.<br>Execute um pipeline para começar a registrar o histórico.</div>';
    ['dora-df','dora-lt','dora-cfr','dora-mttr'].forEach(id => { const el=document.getElementById(id); if(el) el.textContent='—'; });
    ['dora-df-badge','dora-lt-badge','dora-cfr-badge','dora-mttr-badge'].forEach(id => { const el=document.getElementById(id); if(el){ el.textContent=''; el.className='dora-badge'; } });
    return;
  }

  // Compute avg durations for estimate display
  const last5Rel = _history.filter(e => e.pipeline === 'release' && e.outcome === 'success').slice(0, 5);
  const last5Hf  = _history.filter(e => e.pipeline === 'hotfix'  && e.outcome === 'success').slice(0, 5);
  _avgDurations.release = last5Rel.length ? Math.round(last5Rel.reduce((s,e) => s+(e.duration_s||0), 0) / last5Rel.length / 60) : null;
  _avgDurations.hotfix  = last5Hf.length  ? Math.round(last5Hf.reduce((s,e)  => s+(e.duration_s||0), 0) / last5Hf.length  / 60) : null;

  // Deployment Frequency: successful deploys per week over last 30d
  const succRecent = recent.filter(e => e.outcome === 'success').length;
  const df = parseFloat((succRecent / 4.3).toFixed(1));
  const dfEl = document.getElementById('dora-df');
  if (dfEl) dfEl.textContent = df.toFixed(1);
  _setBadge('dora-df-badge', 'df', df);

  // Lead Time: avg duration of successful release pipelines in minutes
  const succRel = recent.filter(e => e.outcome === 'success' && e.pipeline === 'release');
  const lt = succRel.length ? Math.round(succRel.reduce((s,e) => s+(e.duration_s||0), 0) / succRel.length / 60) : null;
  const ltEl = document.getElementById('dora-lt');
  if (ltEl) ltEl.textContent = lt ?? '—';
  _setBadge('dora-lt-badge', 'lt', lt);

  // Change Failure Rate
  const cfr = recent.length ? Math.round(recent.filter(e => e.outcome === 'failed').length / recent.length * 100) : 0;
  const cfrEl = document.getElementById('dora-cfr');
  if (cfrEl) cfrEl.textContent = cfr + '%';
  _setBadge('dora-cfr-badge', 'cfr', cfr);

  // MTTR: avg duration of successful hotfixes in minutes
  const hf = recent.filter(e => e.pipeline === 'hotfix' && e.outcome === 'success');
  const mttr = hf.length ? Math.round(hf.reduce((s,e) => s+(e.duration_s||0), 0) / hf.length / 60) : null;
  const mttrEl = document.getElementById('dora-mttr');
  if (mttrEl) mttrEl.textContent = mttr ?? '—';
  _setBadge('dora-mttr-badge', 'mttr', mttr);

  // Render table
  const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const rows = _history.map(e => {
    const date = new Date(e.started_at).toLocaleString('pt-BR',{day:'2-digit',month:'2-digit',year:'numeric',hour:'2-digit',minute:'2-digit'});
    const pl   = e.pipeline || 'release';
    const plCl = pl === 'hotfix' ? 'hist-pl-hotfix' : 'hist-pl-release';
    const oc   = e.outcome || 'success';
    const stepsOk   = e.steps_ok   ?? 0;
    const stepsFail = e.steps_fail ?? 0;
    const stepsSkip = e.steps_skip ?? 0;
    const stepsStr  = e.steps_total ? `<span style="color:var(--success)">${stepsOk}✓</span> <span style="color:var(--danger)">${stepsFail}✗</span> <span style="color:var(--text3)">${stepsSkip}⊘</span>` : '—';
    const note = e.from_step > 1 ? `<span class="hist-note">retomado da etapa ${e.from_step}</span>` : '';
    return `<tr>
      <td style="font-variant-numeric:tabular-nums">${esc(date)}</td>
      <td><span class="hist-pipeline ${plCl}">${pl}</span></td>
      <td style="font-family:monospace;font-size:11px;color:var(--text2)">${esc(e.version||'—')}</td>
      <td><span class="hist-outcome ${oc}">${oc==='success'?'✓ Sucesso':'✗ Falhou'}</span></td>
      <td style="font-variant-numeric:tabular-nums">${_fmtDur(e.duration_s)}</td>
      <td>${stepsStr}</td>
      <td>${note}</td>
    </tr>`;
  }).join('');

  histEl.innerHTML = `<table class="hist-table">
    <thead><tr>
      <th>Data / Hora</th><th>Pipeline</th><th>Versão</th>
      <th>Resultado</th><th>Duração</th><th>Etapas</th><th></th>
    </tr></thead>
    <tbody>${rows}</tbody>
  </table>`;

  // Update overview mini-widget
  const ovEl = document.getElementById('ov-pipeline-activity');
  if (ovEl) {
    if (!_history.length) {
      ovEl.innerHTML = '<div style="padding:14px 12px;font-size:13px;color:var(--text3)">Nenhuma execução registrada.</div>';
    } else {
      const last5 = _history.slice(0, 5);
      const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');
      ovEl.innerHTML = last5.map(e => {
        const date = new Date(e.started_at).toLocaleString('pt-BR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
        const pl   = e.pipeline || 'release';
        const plCl = pl === 'hotfix' ? 'hist-pl-hotfix' : 'hist-pl-release';
        const oc   = e.outcome || 'success';
        const ocIcon = oc === 'success' ? '<span style="color:var(--success)">✓</span>' : '<span style="color:var(--danger)">✗</span>';
        return `<div style="display:flex;align-items:center;gap:10px;padding:10px 12px;border-bottom:1px solid var(--border);">
          ${ocIcon}
          <span class="hist-pipeline ${plCl}" style="flex-shrink:0">${pl}</span>
          <span style="flex:1;font-size:12px;font-family:monospace;color:var(--text2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(e.version||'—')}</span>
          <span style="font-size:11px;color:var(--text3);flex-shrink:0">${esc(date)}</span>
          <span style="font-size:11px;color:var(--text3);flex-shrink:0;font-variant-numeric:tabular-nums">${_fmtDur(e.duration_s)}</span>
        </div>`;
      }).join('');
    }
  }
}

async function syncJira() {
  const jqlEl    = document.getElementById('jira-modal-jql');
  const statusEl = document.getElementById('jira-modal-status');

  statusEl.style.display = 'none';
  statusEl.textContent = '';
  _jiraPreview = null;
  document.getElementById('jira-modal-conflicts').style.display = 'none';
  document.getElementById('jira-conflicts-list').innerHTML = '';
  document.getElementById('jira-modal-confirm').style.display = '';
  document.getElementById('jira-modal-apply').style.display = 'none';

  try {
    const dr = await fetch('/api/jira/default-jql');
    const dj = await dr.json();
    jqlEl.value = dj.jql || '';
  } catch (e) {
    jqlEl.value = '';
  }

  document.getElementById('jira-modal').classList.add('open');
}

function closeJiraModal() {
  document.getElementById('jira-modal').classList.remove('open');
}

function _setJiraModalStatus(kind, text) {
  const el = document.getElementById('jira-modal-status');
  const palette = {
    info:    {bg: 'var(--info)22',    color: 'var(--info)'},
    success: {bg: 'var(--success)22', color: 'var(--success)'},
    error:   {bg: 'var(--danger)22',  color: 'var(--danger)'},
  }[kind] || {bg: 'var(--text3)22', color: 'var(--text3)'};
  el.style.background = palette.bg;
  el.style.color = palette.color;
  el.textContent = text;
  el.style.display = 'block';
}

// Guarda o resultado de /api/jira/preview entre a etapa de revisão e a de
// aplicação (o usuário decide, por tarefa, se quer sincronizar ou manter o
// que já tinha no dashboard — ex: um card que ele moveu manualmente).
let _jiraPreview = null;

function renderJiraConflicts(conflicts) {
  const list = document.getElementById('jira-conflicts-list');
  list.innerHTML = conflicts.map(c => {
    const changesHtml = c.changes.map(ch =>
      `${esc(ch.label)}: ${esc(ch.old)} → <b>${esc(ch.new)}</b>`
    ).join('<br>');
    return `<label class="checklist-row" style="align-items:flex-start;">
      <input type="checkbox" checked class="jira-conflict-check" data-jira="${esc(c.jira)}">
      <span>
        <div style="font-weight:600;color:var(--text);">${esc(c.jira)} — ${esc(c.title)}</div>
        <div style="color:var(--text3);margin-top:3px;">${changesHtml}</div>
      </span>
    </label>`;
  }).join('');
}

function _toggleAllJiraConflicts(checked) {
  document.querySelectorAll('.jira-conflict-check').forEach(cb => cb.checked = checked);
}

async function previewJiraSync() {
  const jql = document.getElementById('jira-modal-jql').value.trim();
  if (!jql) {
    _setJiraModalStatus('error', 'Informe uma JQL válida.');
    return;
  }

  const confirmBtn = document.getElementById('jira-modal-confirm');
  const cancelBtn  = document.getElementById('jira-modal-cancel');
  confirmBtn.disabled = true;
  cancelBtn.disabled  = true;
  const original = confirmBtn.textContent;
  confirmBtn.textContent = '⟳ Consultando…';
  _setJiraModalStatus('info', 'Consultando o Jira…');
  document.getElementById('jira-modal-conflicts').style.display = 'none';

  try {
    const r = await fetch('/api/jira/preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({jql}),
    });
    const j = await r.json();
    if (!j.ok) {
      _setJiraModalStatus('error',
        '✗ ' + (j.error || 'Falhou') +
        ' — verifique JIRA_BASE_URL, JIRA_EMAIL, JIRA_TOKEN no .env e se os nomes dos campos na JQL conferem com o seu Jira.');
      return;
    }

    _jiraPreview = j;

    if (!j.conflicts.length) {
      // Nada para revisar — só tarefas novas (ou nada mudou). Aplica direto.
      await _doApplyJira([], true);
      return;
    }

    _setJiraModalStatus('info',
      `${j.new_tasks.length} tarefa(s) nova(s) serão adicionadas automaticamente. ` +
      `${j.conflicts.length} já existe(m) e mudaram desde a última importação — revise abaixo.`);

    document.getElementById('jira-conflicts-count').textContent =
      `${j.conflicts.length} tarefa(s) com mudanças`;
    renderJiraConflicts(j.conflicts);
    document.getElementById('jira-modal-conflicts').style.display = 'block';
    confirmBtn.style.display = 'none';
    document.getElementById('jira-modal-apply').style.display = '';
  } catch (e) {
    _setJiraModalStatus('error', '✗ Erro de rede: ' + e.message);
  } finally {
    confirmBtn.disabled = false;
    cancelBtn.disabled  = false;
    confirmBtn.textContent = original;
  }
}

async function applyJiraSync() {
  if (!_jiraPreview) return;
  const acceptedKeys = new Set(
    Array.from(document.querySelectorAll('.jira-conflict-check:checked')).map(cb => cb.dataset.jira)
  );
  const accepted = _jiraPreview.conflicts
    .filter(c => acceptedKeys.has(c.jira))
    .map(c => c.incoming);

  await _doApplyJira(accepted, false);
}

async function _doApplyJira(accepted, autoMode) {
  const applyBtn   = document.getElementById('jira-modal-apply');
  const cancelBtn  = document.getElementById('jira-modal-cancel');
  applyBtn.disabled  = true;
  cancelBtn.disabled = true;
  const originalApply = applyBtn.textContent;
  if (!autoMode) applyBtn.textContent = '⟳ Aplicando…';
  _setJiraModalStatus('info', 'Salvando…');

  try {
    const r = await fetch('/api/jira/apply', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({new_tasks: _jiraPreview.new_tasks, accepted}),
    });
    const j = await r.json();
    if (!j.ok) {
      _setJiraModalStatus('error', '✗ ' + (j.error || 'Falhou ao salvar'));
      return;
    }

    await loadTasks();
    renderKanban();
    renderOverview();
    renderTime();
    renderReleaseComparison();

    const skipped = _jiraPreview.conflicts.length - accepted.length;
    _setJiraModalStatus('success',
      `✓ ${j.created} nova(s), ${j.updated} sincronizada(s)` +
      (skipped > 0 ? `, ${skipped} mantida(s) como estava(m) no dashboard.` : '.'));

    document.getElementById('jira-modal-conflicts').style.display = 'none';
    document.getElementById('jira-modal-confirm').style.display = '';
    applyBtn.style.display = 'none';
  } catch (e) {
    _setJiraModalStatus('error', '✗ Erro de rede: ' + e.message);
  } finally {
    applyBtn.disabled  = false;
    cancelBtn.disabled = false;
    applyBtn.textContent = originalApply;
  }
}

async function loadReport() {
  try {
    const r = await fetch('/api/report');
    if (r.ok) { pipelineSteps = parseReport(await r.text()); }
  } catch(_){}
}

function parseReport(text) {
  const steps = JSON.parse(JSON.stringify(DEFAULT_STEPS));
  for (const line of text.split('\n')) {
    const m = line.match(/\|\s*(✅|❌|⏭|⏳)\s*\|\s*([^|]+)\|([^|]*)\|/);
    if (!m) continue;
    const icon = m[1].trim(), name = m[2].trim(), dur = m[3].trim();
    const status = icon==='✅'?'success':icon==='❌'?'failed':icon==='⏭'?'skipped':'pending';
    const s = steps.find(s => s.name.toLowerCase().includes(name.split(' ')[0].toLowerCase()));
    if (s) { s.status = status; if(dur) s.dur = dur; }
  }
  return steps;
}

function renderAll() {
  renderSidebar();
  renderOverview();
  renderCfgStrip();
  renderStepCards();
  renderPipelineProgress();
  renderDockerInfo();
  renderKanban();
  renderTime();
  renderKPIs();
  renderReleaseComparison();
}

// ── sidebar ───────────────────────────────────────────────────────────────────
function renderSidebar() {
  document.getElementById('sb-rel-branch').textContent = cfg.current_release || '—';
  document.getElementById('sb-rel-meta').textContent   = `v${cfg.crm_version || '—'} · ${cfg.current_sprint || '—'}`;
}

// ── overview ─────────────────────────────────────────────────────────────────
function renderOverview() {
  const sprint = cfg.current_sprint || '';
  document.getElementById('ov-sprint-lbl').textContent =
    sprint ? `${cfg.team_name || 'Time'} · ${sprint}` : cfg.team_name || 'Time';

  const all = tasks;
  const bySprint = sprint ? all.filter(t => t.sprint === sprint) : all;

  document.getElementById('s-total').textContent   = bySprint.length;
  document.getElementById('s-doing').textContent   = bySprint.filter(t=>t.status==='andamento').length;
  document.getElementById('s-review').textContent  = bySprint.filter(t=>t.status==='aguardando').length;
  document.getElementById('s-blocked').textContent = bySprint.filter(t=>t.status==='validacao').length;
  document.getElementById('s-done').textContent    = bySprint.filter(t=>t.status==='concluidas').length;

  renderSprintProgress(bySprint);
  renderReleaseStatus();
  renderTeamWorkload(bySprint);
  renderOvKpiSnap(bySprint);
}

function renderOvKpiSnap(sprintTasks) {
  const el = document.getElementById('ov-kpi-snap');
  if (!el) return;
  const total   = sprintTasks.length;
  const done    = sprintTasks.filter(t => t.status === 'concluidas').length;
  const rate    = total ? Math.round(done / total * 100) : 0;
  const fixes   = sprintTasks.filter(t => t.type === 'fix').length;
  const today   = new Date().toISOString().split('T')[0];
  const overdue = sprintTasks.filter(t => t.due && t.due < today && t.status !== 'concluidas').length;
  const allSprints = [...new Set(tasks.map(t=>t.sprint).filter(Boolean))].sort();
  const sprint  = cfg.current_sprint || '';
  const spIdx   = sprint ? allSprints.indexOf(sprint) : allSprints.length;
  const carry   = tasks.filter(t => allSprints.slice(0, spIdx).includes(t.sprint) && t.status !== 'concluidas').length;
  const rClr    = rate >= 80 ? 'var(--success)' : rate >= 50 ? 'var(--warn)' : 'var(--danger)';
  el.innerHTML = `
    <div style="text-align:center;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);padding:12px 8px">
      <div style="font-size:24px;font-weight:700;color:${rClr}">${rate}%</div>
      <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-top:4px">Taxa de entrega</div>
    </div>
    <div style="text-align:center;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);padding:12px 8px">
      <div style="font-size:24px;font-weight:700;color:var(--warn)">${carry}</div>
      <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-top:4px">Carryover</div>
    </div>
    <div style="text-align:center;background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius);padding:12px 8px">
      <div style="font-size:24px;font-weight:700;color:${overdue>0?'var(--danger)':'var(--text)'}">${overdue}</div>
      <div style="font-size:10px;color:var(--text3);text-transform:uppercase;letter-spacing:.05em;margin-top:4px">Atrasadas</div>
    </div>`;
}

function renderSprintProgress(sprintTasks) {
  const total = sprintTasks.length || 1;
  const el = document.getElementById('sprint-prog');
  el.innerHTML = COLS.map(col => {
    const n = sprintTasks.filter(t => t.status === col.id).length;
    const pct = Math.round((n / total) * 100);
    return `<div class="sp-col-row">
      <span class="sp-col-dot" style="background:${col.color}"></span>
      <span class="sp-col-name">${col.name}</span>
      <div class="sp-bar-wrap"><div class="sp-bar" style="width:${pct}%;background:${col.color}"></div></div>
      <span class="sp-count">${n}</span>
    </div>`;
  }).join('');
}

function renderReleaseStatus() {
  const done  = pipelineSteps.filter(s=>s.status==='success'||s.status==='skipped').length;
  const failed = pipelineSteps.filter(s=>s.status==='failed').length;
  const total = pipelineSteps.length;
  const pct   = Math.round((done/total)*100);
  const fillCls = failed > 0 ? 'failed' : done > 0 && done < total ? 'running' : '';

  document.getElementById('release-status').innerHTML = `
    <div class="rs-row">
      <span class="rs-key">Release</span>
      <span class="rs-val accent">${cfg.version || '—'}</span>
    </div>
    <div class="rs-row" style="margin-top:4px">
      <span class="rs-key">CRM Version</span>
      <span class="rs-val">${cfg.crm_version || '—'}</span>
    </div>
    <div style="margin-top:12px">
      <div class="rs-row" style="margin-bottom:5px">
        <span class="rs-key">Progresso (${done}/${total} etapas)</span>
        <span class="rs-key">${pct}%</span>
      </div>
      <div class="progress-bar"><div class="progress-fill ${fillCls}" style="width:${pct}%"></div></div>
    </div>
    <div class="rs-steps-summary" style="margin-top:12px">
      <span class="rs-chip s">✓ ${pipelineSteps.filter(s=>s.status==='success').length} ok</span>
      ${failed?`<span class="rs-chip f">✕ ${failed} falhou</span>`:''}
      <span class="rs-chip p">○ ${pipelineSteps.filter(s=>s.status==='pending').length} pendente</span>
    </div>
    <div style="margin-top:14px">
      <button class="btn btn-primary" onclick="gotoByName('pipeline')" style="width:100%;font-size:12px;padding:8px">
        ▶ Abrir Pipeline
      </button>
    </div>
  `;
}

function renderTeamWorkload(sprintTasks) {
  const members = [...new Set(sprintTasks.map(t=>t.person).filter(Boolean))].sort();

  if (!members.length) {
    document.getElementById('team-workload').innerHTML =
      '<div style="color:var(--text3);font-size:13px">Nenhuma task com responsável atribuído.</div>';
    return;
  }

  const statuses = COLS.map(c=>c.id);
  const sNames   = Object.fromEntries(COLS.map(c=>[c.id,c.name.replace('Aguardando Autorização para subir','Aguard. Auth')]));
  const sColors  = Object.fromEntries(COLS.map(c=>[c.id,c.color]));

  document.getElementById('team-workload').innerHTML = members.map(person => {
    const myTasks = sprintTasks.filter(t => t.person === person);
    const total   = myTasks.length || 1;

    return `<div class="team-card" onclick="filterByPerson('${esc(person)}')">
      <div class="team-card-top">
        <div class="avatar">${initials(person)}</div>
        <div>
          <div class="tc-name">${esc(person)}</div>
          <div class="tc-count">${myTasks.length} task${myTasks.length!==1?'s':''}</div>
        </div>
      </div>
      <div class="tc-bars">
        ${statuses.map(st => {
          const n = myTasks.filter(t=>t.status===st).length;
          if (!n) return '';
          const pct = Math.round((n/total)*100);
          return `<div class="tc-bar-row">
            <span class="tc-bar-lbl">${sNames[st]}</span>
            <div class="tc-bar-wrap"><div class="tc-bar" style="width:${pct}%;background:${sColors[st]}"></div></div>
            <span class="tc-bar-n">${n}</span>
          </div>`;
        }).join('')}
      </div>
    </div>`;
  }).join('');
}

function filterByPerson(person) {
  document.getElementById('filter-person').value = person;
  gotoByName('esteira');
  renderKanban();
}

// ── pipeline strip ────────────────────────────────────────────────────────────
function renderCfgStrip() {
  document.getElementById('ci-branch').textContent  = cfg.current_release || '—';
  document.getElementById('ci-version').textContent = cfg.version         || '—';
  document.getElementById('ci-crm').textContent     = cfg.crm_version     || '—';
  document.getElementById('ci-next').textContent    = cfg.next_release    || '—';
}

function renderDockerInfo() {
  const reg = cfg.docker_registry || 'registry/img';
  const ver = cfg.version || '—';
  const nxt = cfg.next_version || '—';
  document.getElementById('docker-info').innerHTML = `
    <div class="docker-row">
      <span class="docker-lbl">Tag atual</span>
      <span class="docker-val">${reg}:${ver}</span>
    </div>
    <div class="docker-row">
      <span class="docker-lbl">Tag RC</span>
      <span class="docker-val">${reg}:${nxt}_RC</span>
    </div>
  `;
}

function renderStepCards() {
  const container = document.getElementById('steps-main');
  if (!container) return;
  container.innerHTML = pipelineSteps.map(s => {
    const iconInfo = {
      success:{cls:'si-success',ch:'✓',spin:false},
      failed: {cls:'si-failed', ch:'✕',spin:false},
      running:{cls:'si-running',ch:'◉',spin:true},
      paused: {cls:'si-running',ch:'⏸',spin:false},
      skipped:{cls:'si-skipped',ch:'—',spin:false},
      pending:{cls:'si-pending',ch:String(s.id).padStart(2,'0'),spin:false},
    }[s.status] || {cls:'si-pending',ch:String(s.id).padStart(2,'0'),spin:false};

    const cardCls = {success:'sc-success',failed:'sc-failed',running:'sc-running',paused:'sc-paused',skipped:'sc-skipped'}[s.status]||'';
    const spinCls = iconInfo.spin ? ' sc-spinning' : '';
    const hasLog  = (stepLogs[s.id]||[]).length > 0;

    return `<div class="step-card ${cardCls}" id="sc-${s.id}">
      <div class="sc-header" onclick="toggleStepLog(${s.id})">
        <span class="sc-icon ${iconInfo.cls}${spinCls}">${iconInfo.ch}</span>
        <span class="sc-name">${s.name}</span>
        <span class="sc-dur">${s.dur}</span>
        <button class="sc-log-btn" onclick="event.stopPropagation();toggleStepLog(${s.id})"
          style="${s.status==='pending'&&!hasLog?'visibility:hidden':''}">Log</button>
      </div>
      ${s.error ? `<div class="sc-error open"><span class="log-err">${esc(s.error)}</span>
        <button class="sc-retry-btn" onclick="retryStep(${s.id})">⟳ Reprocessar a partir desta etapa</button>
      </div>` : ''}
      <div class="sc-log" id="sc-log-${s.id}">${(stepLogs[s.id]||[]).map(colorizeLogLine).join('\n')}</div>
    </div>`;
  }).join('');
}

function renderPipelineProgress() {
  const el = document.getElementById('pipeline-progress');
  if (!el) return;
  const done   = pipelineSteps.filter(s=>s.status==='success'||s.status==='skipped').length;
  const failed = pipelineSteps.filter(s=>s.status==='failed').length;
  const run    = pipelineSteps.filter(s=>s.status==='running').length;
  const total  = pipelineSteps.length;
  const pct    = Math.round((done/total)*100);
  const fillCls = failed ? 'failed' : run > 0 ? 'running' : '';
  el.innerHTML = `
    <div style="margin-bottom:6px;display:flex;justify-content:space-between">
      <span style="font-size:11px;color:var(--text3)">Etapas</span>
      <span style="font-size:11px;color:var(--text3)">${done}/${total}</span>
    </div>
    <div class="progress-bar"><div class="progress-fill ${fillCls}" style="width:${pct}%"></div></div>
    <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
      ${done?`<span class="rs-chip s">✓ ${done} ok</span>`:''}
      ${failed?`<span class="rs-chip f">✕ ${failed} falhou</span>`:''}
      ${run?`<span class="rs-chip r">◉ rodando</span>`:''}
      <span class="rs-chip p">○ ${pipelineSteps.filter(s=>s.status==='pending').length} pendente</span>
    </div>
  `;
}

function toggleStepLog(stepId) {
  const log = document.getElementById('sc-log-' + stepId);
  if (log) log.classList.toggle('open');
}

async function retryStep(stepId) {
  const s = pipelineSteps.find(s => s.id === stepId);
  const name = s ? s.name : `Etapa ${stepId}`;
  const ok = await showConfirm(
    `Reprocessar "${name}" (etapa ${stepId})?\n\n` +
    'O pipeline vai retomar do ponto salvo pelo checkpoint.\n' +
    'Etapas já concluídas antes desta serão puladas.\n\n' +
    'Certifique-se de que a VPN está conectada antes de confirmar.',
    {icon:'🔁', title:'Reprocessar etapa', okLabel:'Reprocessar'}
  );
  if (!ok) return;

  try {
    const r = await fetch('/api/release/retry-step', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({step: stepId}),
    });
    const j = await r.json();
    if (!j.ok) { showAlert('Erro ao reprocessar: ' + (j.error || 'Falhou'), {icon:'❌', title:'Erro'}); return; }
    activePipeline = 'release';
    connectStream();
  } catch(e) {
    showAlert('Erro de rede: ' + e.message, {icon:'❌', title:'Erro de rede'});
  }
}

function _updateStepCard(stepId, status, dur, error) {
  const s = pipelineSteps.find(s=>s.id===stepId);
  if (!s) return;
  s.status = status;
  if (dur)   s.dur   = dur;
  if (error) s.error = error;

  const card = document.getElementById('sc-' + stepId);
  if (!card) { renderStepCards(); return; }

  card.className = 'step-card ' + ({success:'sc-success',failed:'sc-failed',running:'sc-running',paused:'sc-paused',skipped:'sc-skipped'}[status]||'');

  const iconInfo = {
    success:{cls:'si-success',ch:'✓',spin:false},
    failed: {cls:'si-failed', ch:'✕',spin:false},
    running:{cls:'si-running',ch:'◉',spin:true},
    paused: {cls:'si-running',ch:'⏸',spin:false},
    skipped:{cls:'si-skipped',ch:'—',spin:false},
    pending:{cls:'si-pending',ch:String(stepId).padStart(2,'0'),spin:false},
  }[status] || {cls:'si-pending',ch:String(stepId).padStart(2,'0'),spin:false};

  const icon = card.querySelector('.sc-icon');
  icon.className = 'sc-icon ' + iconInfo.cls + (iconInfo.spin?' sc-spinning':'');
  icon.textContent = iconInfo.ch;
  card.querySelector('.sc-dur').textContent = dur || s.dur;
  const logBtn = card.querySelector('.sc-log-btn');
  if (logBtn) logBtn.style.visibility = (status==='pending' ? 'hidden' : '');

  // Error box
  let errEl = card.querySelector('.sc-error');
  if (error) {
    if (!errEl) {
      errEl = document.createElement('div');
      errEl.className = 'sc-error open';
      card.querySelector('.sc-header').after(errEl);
    }
    errEl.innerHTML = `<span class="log-err">${esc(error)}</span>` +
      `<button class="sc-retry-btn" onclick="retryStep(${stepId})">⟳ Reprocessar a partir desta etapa</button>`;
    errEl.classList.add('open');
  } else if (errEl) {
    errEl.classList.remove('open');
  }

  if (status === 'running') {
    card.scrollIntoView({behavior:'smooth', block:'nearest'});
  }

  renderPipelineProgress();
  renderReleaseStatus();
}

function colorizeLogLine(line) {
  const e = esc(line);
  if (/^\s*✓/.test(line)) return `<span class="log-ok">${e}</span>`;
  if (/^\s*✗/.test(line)) return `<span class="log-err">${e}</span>`;
  if (/^\s*→/.test(line)) return `<span class="log-info">${e}</span>`;
  if (/^\s*⚠|^\s*Warning/i.test(line)) return `<span class="log-warn">${e}</span>`;
  return `<span>${e}</span>`;
}

function _appendStepLog(stepId, line) {
  if (!stepLogs[stepId]) stepLogs[stepId] = [];
  const clean = ansiStrip(line);
  if (!clean) return;
  stepLogs[stepId].push(clean);
  if (stepLogs[stepId].length > 200) stepLogs[stepId].shift();
  const el = document.getElementById('sc-log-' + stepId);
  if (el) {
    el.innerHTML = stepLogs[stepId].map(colorizeLogLine).join('\n');
    el.scrollTop = el.scrollHeight;
  }
}

function resetStepCards() {
  pipelineSteps = JSON.parse(JSON.stringify(DEFAULT_STEPS));
  stepLogs      = {};
  _currentStep  = 0;
  _stepStartTs  = {};
  renderStepCards();
  renderPipelineProgress();
}

// ── hotfix step cards (mesmo padrão dos cards de release, estado isolado) ─────
function renderHotfixStepCards() {
  const container = document.getElementById('steps-hotfix');
  if (!container) return;
  container.innerHTML = hotfixStepsState.map(s => {
    const iconInfo = {
      success:{cls:'si-success',ch:'✓',spin:false},
      failed: {cls:'si-failed', ch:'✕',spin:false},
      running:{cls:'si-running',ch:'◉',spin:true},
      paused: {cls:'si-running',ch:'⏸',spin:false},
      skipped:{cls:'si-skipped',ch:'—',spin:false},
      pending:{cls:'si-pending',ch:String(s.id).padStart(2,'0'),spin:false},
    }[s.status] || {cls:'si-pending',ch:String(s.id).padStart(2,'0'),spin:false};

    const cardCls = {success:'sc-success',failed:'sc-failed',running:'sc-running',paused:'sc-paused',skipped:'sc-skipped'}[s.status]||'';
    const spinCls = iconInfo.spin ? ' sc-spinning' : '';
    const hasLog  = (hotfixStepLogs[s.id]||[]).length > 0;

    return `<div class="step-card ${cardCls}" id="hsc-${s.id}">
      <div class="sc-header" onclick="toggleHotfixStepLog(${s.id})">
        <span class="sc-icon ${iconInfo.cls}${spinCls}">${iconInfo.ch}</span>
        <span class="sc-name">${s.name}</span>
        <span class="sc-dur">${s.dur}</span>
        <button class="sc-log-btn" onclick="event.stopPropagation();toggleHotfixStepLog(${s.id})"
          style="${s.status==='pending'&&!hasLog?'visibility:hidden':''}">Log</button>
      </div>
      ${s.error ? `<div class="sc-error open"><span class="log-err">${esc(s.error)}</span>
        <button class="sc-retry-btn" onclick="retryHotfixStep(${s.id})">⟳ Reprocessar a partir desta etapa</button>
      </div>` : ''}
      <div class="sc-log" id="hsc-log-${s.id}">${(hotfixStepLogs[s.id]||[]).map(colorizeLogLine).join('\n')}</div>
    </div>`;
  }).join('');
}

function renderHotfixPipelineProgress() {
  const el = document.getElementById('hotfix-pipeline-progress');
  if (!el) return;
  const done   = hotfixStepsState.filter(s=>s.status==='success'||s.status==='skipped').length;
  const failed = hotfixStepsState.filter(s=>s.status==='failed').length;
  const run    = hotfixStepsState.filter(s=>s.status==='running').length;
  const total  = hotfixStepsState.length;
  const pct    = Math.round((done/total)*100);
  const fillCls = failed ? 'failed' : run > 0 ? 'running' : '';
  el.innerHTML = `
    <div style="margin-bottom:6px;display:flex;justify-content:space-between">
      <span style="font-size:11px;color:var(--text3)">Etapas</span>
      <span style="font-size:11px;color:var(--text3)">${done}/${total}</span>
    </div>
    <div class="progress-bar"><div class="progress-fill ${fillCls}" style="width:${pct}%"></div></div>
    <div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">
      ${done?`<span class="rs-chip s">✓ ${done} ok</span>`:''}
      ${failed?`<span class="rs-chip f">✕ ${failed} falhou</span>`:''}
      ${run?`<span class="rs-chip r">◉ rodando</span>`:''}
      <span class="rs-chip p">○ ${hotfixStepsState.filter(s=>s.status==='pending').length} pendente</span>
    </div>
  `;
}

function toggleHotfixStepLog(stepId) {
  const log = document.getElementById('hsc-log-' + stepId);
  if (log) log.classList.toggle('open');
}

async function retryHotfixStep(stepId) {
  const s = hotfixStepsState.find(s => s.id === stepId);
  const name = s ? s.name : `Etapa ${stepId}`;
  const ok = await showConfirm(
    `Reprocessar "${name}" (etapa ${stepId}) do hotfix?\n\n` +
    'O pipeline vai retomar do ponto salvo pelo checkpoint.\n' +
    'Certifique-se de que a VPN está conectada antes de confirmar.',
    {icon:'🔁', title:'Reprocessar etapa', okLabel:'Reprocessar'}
  );
  if (!ok) return;

  try {
    const r = await fetch('/api/hotfix/retry-step', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({step: stepId}),
    });
    const j = await r.json();
    if (!j.ok) { showAlert('Erro ao reprocessar: ' + (j.error || 'Falhou'), {icon:'❌', title:'Erro'}); return; }
    activePipeline = 'hotfix';
    connectStream();
  } catch(e) {
    showAlert('Erro de rede: ' + e.message, {icon:'❌', title:'Erro de rede'});
  }
}

function _updateHotfixStepCard(stepId, status, dur, error) {
  const s = hotfixStepsState.find(s=>s.id===stepId);
  if (!s) return;
  s.status = status;
  if (dur)   s.dur   = dur;
  if (error) s.error = error;

  const card = document.getElementById('hsc-' + stepId);
  if (!card) { renderHotfixStepCards(); return; }

  card.className = 'step-card ' + ({success:'sc-success',failed:'sc-failed',running:'sc-running',paused:'sc-paused',skipped:'sc-skipped'}[status]||'');

  const iconInfo = {
    success:{cls:'si-success',ch:'✓',spin:false},
    failed: {cls:'si-failed', ch:'✕',spin:false},
    running:{cls:'si-running',ch:'◉',spin:true},
    paused: {cls:'si-running',ch:'⏸',spin:false},
    skipped:{cls:'si-skipped',ch:'—',spin:false},
    pending:{cls:'si-pending',ch:String(stepId).padStart(2,'0'),spin:false},
  }[status] || {cls:'si-pending',ch:String(stepId).padStart(2,'0'),spin:false};

  const icon = card.querySelector('.sc-icon');
  icon.className = 'sc-icon ' + iconInfo.cls + (iconInfo.spin?' sc-spinning':'');
  icon.textContent = iconInfo.ch;
  card.querySelector('.sc-dur').textContent = dur || s.dur;
  const logBtn = card.querySelector('.sc-log-btn');
  if (logBtn) logBtn.style.visibility = (status==='pending' ? 'hidden' : '');

  let errEl = card.querySelector('.sc-error');
  if (error) {
    if (!errEl) {
      errEl = document.createElement('div');
      errEl.className = 'sc-error open';
      card.querySelector('.sc-header').after(errEl);
    }
    errEl.innerHTML = `<span class="log-err">${esc(error)}</span>` +
      `<button class="sc-retry-btn" onclick="retryHotfixStep(${stepId})">⟳ Reprocessar a partir desta etapa</button>`;
    errEl.classList.add('open');
  } else if (errEl) {
    errEl.classList.remove('open');
  }

  if (status === 'running') {
    card.scrollIntoView({behavior:'smooth', block:'nearest'});
  }

  renderHotfixPipelineProgress();
}

function _appendHotfixStepLog(stepId, line) {
  if (!hotfixStepLogs[stepId]) hotfixStepLogs[stepId] = [];
  const clean = ansiStrip(line);
  if (!clean) return;
  hotfixStepLogs[stepId].push(clean);
  if (hotfixStepLogs[stepId].length > 200) hotfixStepLogs[stepId].shift();
  const el = document.getElementById('hsc-log-' + stepId);
  if (el) {
    el.innerHTML = hotfixStepLogs[stepId].map(colorizeLogLine).join('\n');
    el.scrollTop = el.scrollHeight;
  }
}

function resetHotfixStepCards() {
  hotfixStepsState   = JSON.parse(JSON.stringify(DEFAULT_HOTFIX_STEPS));
  hotfixStepLogs     = {};
  _hotfixCurrentStep = 0;
  _hotfixStepStartTs = {};
  renderHotfixStepCards();
  renderHotfixPipelineProgress();
}

function setHotfixRunningUI(running, paused = false) {
  document.getElementById('btn-hotfix-run').disabled   = running;
  document.getElementById('btn-hotfix-pause').disabled = !running;
  document.getElementById('btn-hotfix-stop').disabled  = !running;
  const pauseBtn = document.getElementById('btn-hotfix-pause');
  pauseBtn.textContent = paused ? '▶ Continuar' : '⏸ Pausar';
  pauseBtn.className   = paused ? 'btn btn-primary' : 'btn btn-warning';
  const dot = document.getElementById('hotfix-run-dot');
  const lbl = document.getElementById('hotfix-run-label');
  dot.className = 'status-dot' + (running ? (paused ? ' paused' : ' running') : '');
  lbl.textContent = running ? (paused ? 'Pausado ⏸' : 'Rodando…') : 'Ocioso';
}

function toggleHotfixPreviousFields() {
  const checked = document.getElementById('hz-previous').checked;
  document.getElementById('hz-current-release-row').style.display = checked ? '' : 'none';
}

// ── checklist manual pós-hotfix (passos que não dão para automatizar:  ────────
//    formalização no Slack, planilha de releases, atualização no Rancher) ─────
const HOTFIX_CHECKLIST_ITEMS = [
  {id:'formalizar', label:'Formalizar a entrega do hotfix no canal #team-frontend (com 🏆)'},
  {id:'planilha',   label:'Atualizar a planilha de releases com a versão liberada'},
  {id:'rancher',    label:'Atualizar os ambientes no Rancher com a imagem do hotfix'},
];

function _hotfixChecklistKey() {
  const el = document.getElementById('hz-version');
  const v = (el ? el.value : '').trim();
  return 'zcrm_hotfix_checklist_' + (v || 'default');
}

function _loadHotfixChecklistState() {
  try { return JSON.parse(localStorage.getItem(_hotfixChecklistKey())) || {}; }
  catch(e) { return {}; }
}

function renderHotfixChecklist() {
  const el = document.getElementById('hotfix-checklist');
  if (!el) return;
  const state = _loadHotfixChecklistState();
  el.innerHTML = HOTFIX_CHECKLIST_ITEMS.map(item => {
    const checked = !!state[item.id];
    return `<label class="checklist-row ${checked?'done':''}">
      <input type="checkbox" ${checked?'checked':''} onchange="toggleHotfixChecklistItem('${item.id}', this.checked)">
      <span>${esc(item.label)}</span>
    </label>`;
  }).join('');
}

function toggleHotfixChecklistItem(id, checked) {
  const state = _loadHotfixChecklistState();
  state[id] = checked;
  localStorage.setItem(_hotfixChecklistKey(), JSON.stringify(state));
  renderHotfixChecklist();
}

async function startHotfix() {
  const branch  = document.getElementById('hz-branch').value.trim();
  const target  = document.getElementById('hz-target').value.trim();
  const version = document.getElementById('hz-version').value.trim();
  if (!branch || !target || !version) {
    await showAlert('Preencha ao menos a branch de hotfix, a release alvo e a versão do hotfix.', {icon:'⚠️', title:'Campos obrigatórios'});
    return;
  }

  const isPrevious = document.getElementById('hz-previous').checked;
  const currentRel = document.getElementById('hz-current-release').value.trim();
  if (isPrevious && !currentRel) {
    await showAlert('Informe a branch da release atual para propagar o hotfix.', {icon:'⚠️', title:'Campo obrigatório'});
    return;
  }

  const fromStep = parseInt(document.getElementById('hz-from-step').value) || 1;

  const payload = {
    hotfix_branch: branch,
    target_release: target,
    hotfix_version: version,
    ticket: document.getElementById('hz-ticket').value.trim(),
    description: document.getElementById('hz-description').value.trim(),
    is_previous_release: isPrevious,
    current_release: currentRel,
    from_step: fromStep,
  };

  const stepLabel = fromStep > 1
    ? `\nIniciando a partir da etapa ${String(fromStep).padStart(2,'0')}`
    : '';

  const ok = await showConfirm(
    `Iniciar pipeline de hotfix para ${version}?\n\n` +
    `Branch: ${branch}\nRelease alvo: ${target}` +
    (isPrevious ? `\nSerá propagado para: ${currentRel}` : '') +
    stepLabel +
    `\n\nCertifique-se de que a VPN está conectada antes de confirmar.`,
    {icon:'🚀', title:'Iniciar hotfix', okLabel:'Iniciar'}
  );
  if (!ok) return;

  activePipeline = 'hotfix';
  resetHotfixStepCards();

  try {
    const r = await fetch('/api/hotfix/run', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!j.ok) { await showAlert('Erro ao iniciar: ' + (j.error||'Falhou'), {icon:'❌', title:'Erro'}); return; }
    setHotfixRunningUI(true);
    connectStream();
  } catch(e) {
    await showAlert('Erro de rede: ' + e.message, {icon:'❌', title:'Erro de rede'});
  }
}

async function stopHotfix() {
  const ok = await showConfirm('Parar o hotfix em execução?', {icon:'⛔', title:'Parar hotfix', okLabel:'Parar', okCls:'danger'});
  if (!ok) return;
  await fetch('/api/release/stop', {method:'POST'});
}

// ── release run/stop ──────────────────────────────────────────────────────────
function setRunningUI(running, paused = false) {
  document.getElementById('btn-run').disabled   = running;
  document.getElementById('btn-pause').disabled = !running;
  document.getElementById('btn-stop').disabled  = !running;
  const pauseBtn = document.getElementById('btn-pause');
  pauseBtn.textContent = paused ? '▶ Continuar' : '⏸ Pausar';
  pauseBtn.className   = paused ? 'btn btn-primary' : 'btn btn-warning';
  const dot = document.getElementById('run-dot');
  const lbl = document.getElementById('run-label');
  dot.className = 'status-dot' + (running ? (paused ? ' paused' : ' running') : '');
  lbl.textContent = running ? (paused ? 'Pausado ⏸' : 'Rodando…') : 'Ocioso';
  const ind = document.getElementById('sb-running');
  ind.classList.toggle('visible', running);
}

// ── live timer ────────────────────────────────────────────────────────────────
let _timerInterval = null;
let _timerStart    = null;

function _fmtTimer(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  const ss = String(s % 60).padStart(2, '0');
  const mm = String(m % 60).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${m}:${ss}`;
}

function _startTimer(isHotfix) {
  _timerStart = Date.now();
  const timerId = isHotfix ? 'hotfix-run-timer' : 'run-timer';
  const estId   = isHotfix ? 'hotfix-run-estimate' : 'run-estimate';
  const timerEl = document.getElementById(timerId);
  const estEl   = document.getElementById(estId);
  if (timerEl) timerEl.style.display = '';
  if (estEl) {
    const avgMin = _avgDurations[isHotfix ? 'hotfix' : 'release'];
    if (avgMin) {
      estEl.textContent = `/ estimado ~${avgMin}min`;
      estEl.style.display = '';
    }
  }
  if (_timerInterval) clearInterval(_timerInterval);
  _timerInterval = setInterval(() => {
    if (timerEl) timerEl.textContent = '⏱ ' + _fmtTimer(Date.now() - _timerStart);
  }, 1000);
}

function _stopTimer(isHotfix) {
  if (_timerInterval) { clearInterval(_timerInterval); _timerInterval = null; }
  const timerId = isHotfix ? 'hotfix-run-timer' : 'run-timer';
  const estId   = isHotfix ? 'hotfix-run-estimate' : 'run-estimate';
  const el = document.getElementById(timerId);
  const estEl = document.getElementById(estId);
  if (el) el.style.display = 'none';
  if (estEl) estEl.style.display = 'none';
}

// avg durations per pipeline type (minutes), populated by renderHistory
let _avgDurations = { release: null, hotfix: null };

// ── browser notifications ─────────────────────────────────────────────────────
function _requestNotifPerm() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

function _notif(title, body, icon) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  try {
    new Notification(title, { body: body || '', icon: icon || '' });
  } catch(e) {}
}

function connectStream() {
  if (es) { es.close(); es = null; }
  es = new EventSource('/api/release/stream');
  es.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.t === 'ping') return;

    const isHotfix = activePipeline === 'hotfix';

    if (msg.t === 'start') {
      const fromStep = msg.from_step || 0;
      _requestNotifPerm();
      _startTimer(isHotfix);
      if (isHotfix) {
        resetHotfixStepCards();
        setHotfixRunningUI(true);
        if (fromStep > 1) {
          for (let i = 1; i < fromStep; i++) _updateHotfixStepCard(i, 'success', '—');
        }
      } else {
        resetStepCards();
        setRunningUI(true);
        if (fromStep > 1) {
          for (let i = 1; i < fromStep; i++) _updateStepCard(i, 'success', '—');
        }
      }

    } else if (msg.t === 'step_start') {
      if (isHotfix) {
        _hotfixCurrentStep = msg.step;
        _hotfixStepStartTs[msg.step] = Date.now();
        _updateHotfixStepCard(msg.step, 'running');
      } else {
        _currentStep = msg.step;
        _stepStartTs[msg.step] = Date.now();
        _updateStepCard(msg.step, 'running');
      }

    } else if (msg.t === 'step_done') {
      if (isHotfix) {
        const elapsed = _hotfixStepStartTs[msg.step]
          ? ((Date.now() - _hotfixStepStartTs[msg.step]) / 1000).toFixed(1) + 's'
          : '—';
        _updateHotfixStepCard(msg.step, msg.status, elapsed, msg.error || null);
      } else {
        const elapsed = _stepStartTs[msg.step]
          ? ((Date.now() - _stepStartTs[msg.step]) / 1000).toFixed(1) + 's'
          : '—';
        _updateStepCard(msg.step, msg.status, elapsed, msg.error || null);
      }

    } else if (msg.t === 'step_fail_pause') {
      showStepFailModal(msg.step, msg.error || '', isHotfix);
      _notif('⚠️ Pipeline parou — ação necessária',
        `Etapa ${msg.step} falhou: ${(msg.error||'').slice(0, 120)}`);

    } else if (msg.t === 'paused') {
      if (isHotfix) setHotfixRunningUI(true, true);
      else          setRunningUI(true, true);

    } else if (msg.t === 'resumed') {
      if (isHotfix) setHotfixRunningUI(true, false);
      else          setRunningUI(true, false);

    } else if (msg.t === 'conflict_pause') {
      const conflictStep = msg.step || (isHotfix ? _hotfixCurrentStep : _currentStep);
      showConflictModal(msg.branches || [], conflictStep);
      if (isHotfix) _updateHotfixStepCard(conflictStep, 'paused');
      else          _updateStepCard(conflictStep, 'paused');
      _notif('🔀 Conflito de merge detectado',
        `Etapa ${conflictStep} — ${(msg.branches||[]).join(', ')}`);

    } else if (msg.t === 'line') {
      if (isHotfix) {
        const stepId = msg.step || _hotfixCurrentStep;
        if (stepId) _appendHotfixStepLog(stepId, msg.v);
      } else {
        const stepId = msg.step || _currentStep;
        if (stepId) _appendStepLog(stepId, msg.v);
      }

    } else if (msg.t === 'done') {
      _lastExitCode = msg.code;
      const ok  = msg.code === 0;
      const dotId = isHotfix ? 'hotfix-run-dot' : 'run-dot';
      const lblId = isHotfix ? 'hotfix-run-label' : 'run-label';
      const dot = document.getElementById(dotId);
      dot.className = 'status-dot ' + (ok ? 'done-ok' : 'done-err');
      document.getElementById(lblId).textContent =
        ok ? 'Concluído ✓' : 'Falhou ✕ (código ' + msg.code + ')';
      const plName = isHotfix ? 'Hotfix' : 'Release';
      _notif(ok ? `✅ Pipeline ${plName} concluída` : `❌ Pipeline ${plName} falhou`,
        ok ? 'Todas as etapas finalizadas com sucesso.' : 'Verifique o dashboard para detalhes.');

    } else if (msg.t === 'end') {
      _stopTimer(isHotfix);
      loadHistory();   // auto-refresh history & overview widget
      if (isHotfix) {
        setHotfixRunningUI(false);
        renderHotfixStepCards();
        renderHotfixPipelineProgress();
      } else {
        setRunningUI(false);
        if (_lastExitCode === 0) {
          loadReport().then(() => { renderStepCards(); renderReleaseStatus(); renderPipelineProgress(); });
        } else {
          renderStepCards(); renderReleaseStatus(); renderPipelineProgress();
        }
      }
      _lastExitCode = null;
      if (es) { es.close(); es = null; }

    } else if (msg.t === 'error') {
      if (isHotfix) { if (_hotfixCurrentStep) _appendHotfixStepLog(_hotfixCurrentStep, '❌ ' + msg.v); }
      else          { if (_currentStep) _appendStepLog(_currentStep, '❌ ' + msg.v); }
    }
  };
  es.onerror = () => { if(es){es.close();es=null;} };
}

// ── step failure modal ────────────────────────────────────────────────────────
let _sfmIsHotfix = false;

const _STEP_HINTS = {
  11: () => {
    const repo = cfg.repo_path || '~/Documentos/zcrm_front';
    return {
      title: 'Build Docker falhou',
      cmd: `cd ${repo}\ndocker build -f Dockerfile -t zcrm_front .`,
      note: 'Rode o comando acima no terminal (precisa ter o grupo docker ativo: newgrp docker) e depois clique em "Já fiz, continuar".',
    };
  },
  12: () => {
    const reg  = cfg.docker_registry || 'zanthusinovacao1/zcrm_front';
    const ver  = cfg.version || '<versao>';
    const tag  = `${reg}:${ver}`;
    return {
      title: 'Docker Push falhou',
      cmd: `docker tag zcrm_front:latest ${tag}\ndocker push ${tag}`,
      note: 'Rode os comandos acima no terminal (precisa ter o grupo docker ativo: newgrp docker) e depois clique em "Já fiz, continuar".',
    };
  },
  15: () => {
    const reg = cfg.docker_registry || 'zanthusinovacao1/zcrm_front';
    const ver = cfg.version || '<versao>';
    return {
      title: 'Gerar imagem RC falhou',
      cmd: `docker build -f Dockerfile -t zcrm_front .\ndocker tag zcrm_front:latest ${reg}:${ver}_RC\ndocker push ${reg}:${ver}_RC`,
      note: 'Rode os comandos acima no terminal (precisa ter o grupo docker ativo: newgrp docker) e depois clique em "Já fiz, continuar".',
    };
  },
  16: () => {
    const nextRelease = cfg.next_release || '<next_release>';
    const ver = cfg.version || '<versao>';
    const semver = ver.split('_')[0];
    return {
      title: 'Criar branch dev falhou',
      cmd: `git checkout ${nextRelease}\ngit checkout -b dev/${semver}\ngit push -u origin dev/${semver}`,
      note: 'Crie a branch dev manualmente no terminal e depois clique em "Já fiz, continuar".',
    };
  },
  17: () => ({
    title: 'Anúncio no Slack falhou',
    cmd: '',
    note: 'Verifique o SLACK_TOKEN no arquivo .env e se o bot tem permissão de postar no canal #team-frontend. Clique em "Já fiz, continuar" após enviar o anúncio manualmente.',
  }),
};

function showStepFailModal(stepNum, errorMsg, isHotfix) {
  _sfmIsHotfix = isHotfix;
  document.getElementById('sfm-step-num').textContent = String(stepNum).padStart(2,'0');
  document.getElementById('sfm-error').textContent = errorMsg || 'Verifique o log da etapa para detalhes.';

  const hintFn = _STEP_HINTS[stepNum];
  const hintBox = document.getElementById('sfm-hint-box');
  if (hintFn) {
    const h = hintFn();
    hintBox.innerHTML = `
      <div style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--text3);margin-bottom:6px">${esc(h.title)} — comando manual</div>
      <pre style="background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:10px 14px;font-size:11.5px;color:var(--text);margin:0 0 8px;overflow-x:auto">${esc(h.cmd)}</pre>
      <div style="font-size:11.5px;color:var(--text2)">${esc(h.note)}</div>
    `;
    hintBox.style.display = 'block';
    document.getElementById('sfm-skip-btn').querySelector('strong').textContent = '⏭ Já fiz manualmente, continuar';
  } else {
    hintBox.style.display = 'none';
    document.getElementById('sfm-skip-btn').querySelector('strong').textContent = '⏭ Pular esta etapa e continuar';
  }

  document.getElementById('sfm-skip-btn').disabled  = false;
  document.getElementById('sfm-abort-btn').disabled = false;
  document.getElementById('step-fail-modal').classList.add('open');
}

async function stepFailDecision(decision) {
  document.getElementById('sfm-skip-btn').disabled  = true;
  document.getElementById('sfm-abort-btn').disabled = true;
  const endpoint = _sfmIsHotfix ? '/api/hotfix/fail-decision' : '/api/release/fail-decision';
  try {
    await fetch(endpoint, {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({decision}),
    });
  } catch(e) { /* ignora erros de rede */ }
  document.getElementById('step-fail-modal').classList.remove('open');
}

// ── rollback modal ────────────────────────────────────────────────────────────
let _rollbackTargetSha = '';

async function showRollbackModal() {
  _rollbackTargetSha = '';
  document.getElementById('rollback-info-box').innerHTML = '<div style="color:var(--text3);font-size:12px">Carregando informações…</div>';
  document.getElementById('rollback-commits-box').style.display = 'none';
  document.getElementById('rollback-confirm-btn').disabled = true;
  document.getElementById('rollback-modal').classList.add('open');

  try {
    const r = await fetch('/api/release/rollback-info');
    const j = await r.json();
    if (!j.ok) {
      document.getElementById('rollback-info-box').innerHTML =
        `<div style="color:var(--danger);font-size:13px">❌ ${esc(j.error)}</div>`;
      return;
    }
    if (!j.commits.length) {
      document.getElementById('rollback-info-box').innerHTML =
        `<div style="color:var(--text2);font-size:13px">Nenhum commit local encontrado — a pipeline não deixou commits para desfazer.</div>`;
      return;
    }

    _rollbackTargetSha = j.starting_head || '';

    document.getElementById('rollback-info-box').innerHTML =
      `<div style="background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.25);border-radius:6px;padding:8px 12px;font-size:12.5px;color:var(--text2)">
         Release: <strong style="color:var(--text)">${esc(j.current_release)}</strong> — ${j.commits.length} commit(s) da pipeline para desfazer
       </div>`;

    document.getElementById('rollback-commits-list').innerHTML = j.commits.map(c =>
      `<div style="display:flex;align-items:center;gap:8px">
         <span style="color:var(--danger);font-size:11px">−</span>
         <span style="color:var(--text)">${esc(c)}</span>
       </div>`
    ).join('');
    document.getElementById('rollback-target-sha').textContent = _rollbackTargetSha.slice(0,8) || '—';
    document.getElementById('rollback-commits-box').style.display = 'block';
    document.getElementById('rollback-confirm-btn').disabled = !_rollbackTargetSha;
  } catch(e) {
    document.getElementById('rollback-info-box').innerHTML =
      `<div style="color:var(--danger);font-size:13px">❌ Erro de rede: ${esc(e.message)}</div>`;
  }
}

function closeRollbackModal() {
  document.getElementById('rollback-modal').classList.remove('open');
}

async function confirmRollback() {
  if (!_rollbackTargetSha) return;
  const btn = document.getElementById('rollback-confirm-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Revertendo…';
  try {
    const r = await fetch('/api/release/rollback', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({target_sha: _rollbackTargetSha}),
    });
    const j = await r.json();
    if (j.ok) {
      closeRollbackModal();
      await showAlert(j.message || 'Rollback aplicado com sucesso.', {icon:'✅', title:'Revertido'});
      resetStepCards();
      renderReleaseStatus();
    } else {
      await showAlert('Erro ao reverter: ' + (j.error || 'Falhou'), {icon:'❌', title:'Erro'});
    }
  } catch(e) {
    await showAlert('Erro de rede: ' + e.message, {icon:'❌', title:'Erro'});
  } finally {
    btn.disabled = false;
    btn.textContent = '↩ Reverter commits locais';
  }
}

// ── conflict modal ────────────────────────────────────────────────────────────
function showConflictModal(branches, stepNum) {
  const list = document.getElementById('conflict-branch-list');
  if (!branches.length) {
    list.innerHTML = `<div style="color:var(--text3);font-size:12px;font-style:italic">Nenhuma branch listada — verifique o log da etapa.</div>`;
  } else {
    list.innerHTML = branches.map(b =>
      `<div style="display:flex;align-items:center;gap:10px">
         <span style="color:var(--danger);font-size:13px;flex-shrink:0">✗</span>
         <code style="color:var(--text);font-size:12px;word-break:break-all">${esc(b)}</code>
       </div>`
    ).join('');
  }
  const badge = document.getElementById('conflict-step-badge');
  if (stepNum) {
    badge.textContent = `⏸ Pipeline pausada na etapa ${String(stepNum).padStart(2,'0')}`;
  } else {
    badge.textContent = '⏸ Pipeline pausada nesta etapa';
  }
  document.getElementById('conflict-modal').classList.add('open');
}

function closeConflictModal() {
  document.getElementById('conflict-modal').classList.remove('open');
}

async function confirmConflictResolved() {
  const btn = document.getElementById('conflict-confirm-btn');
  btn.disabled = true;
  btn.textContent = '⏳ Retomando pipeline…';
  try {
    await fetch('/api/release/conflict-resume', {method:'POST'});
    closeConflictModal();
  } catch(e) {
    await showAlert('Erro ao retomar: ' + e.message, {icon:'❌', title:'Erro'});
  } finally {
    btn.disabled = false;
    btn.textContent = '✓ Confirmar resolução e continuar';
  }
}

// ── wizard ────────────────────────────────────────────────────────────────────
function startRelease() {
  document.getElementById('wz-cur-branch').value  = cfg.current_release || '';
  document.getElementById('wz-version').value     = cfg.version         || '';
  document.getElementById('wz-crm').value         = cfg.crm_version     || '';
  document.getElementById('wz-next-branch').value = cfg.next_release    || '';
  document.getElementById('wz-next-crm').value    = cfg.next_crm        || '';
  document.getElementById('wz-branches').value    = '';
  document.getElementById('wz-from-step').value   = '1';
  onWzStepChange();
  document.getElementById('wizard-modal').classList.add('open');
}

function onWzStepChange() {
  const step = parseInt(document.getElementById('wz-from-step').value, 10);
  const isResume = step > 1;
  document.getElementById('wz-resume-notice').style.display = isResume ? 'block' : 'none';
  document.getElementById('wz-actions-box').style.display   = isResume ? 'none'  : 'block';
  const btn = document.getElementById('wz-confirm-btn');
  btn.textContent = isResume
    ? `▶ Retomar da etapa ${String(step).padStart(2,'0')}`
    : '▶ Confirmar e Iniciar';
}

function closeWizard() { document.getElementById('wizard-modal').classList.remove('open'); }

async function confirmWizard() {
  const fromStep = parseInt(document.getElementById('wz-from-step').value, 10);
  const btn = document.getElementById('wz-confirm-btn');
  btn.disabled = true;
  const isResume = fromStep > 1;
  const stepLabel = String(fromStep).padStart(2,'0');
  btn.textContent = isResume ? `Retomando etapa ${stepLabel}…` : 'Iniciando…';

  const rawBr  = document.getElementById('wz-branches').value.trim();
  const branches = rawBr ? rawBr.split(',').map(b=>b.trim()).filter(Boolean) : [];

  const payload = {
    current_release:  document.getElementById('wz-cur-branch').value.trim(),
    frontend_version: document.getElementById('wz-version').value.trim(),
    crm_version:      document.getElementById('wz-crm').value.trim(),
    next_release:     document.getElementById('wz-next-branch').value.trim(),
    next_crm_version: document.getElementById('wz-next-crm').value.trim(),
    branches,
    from_step: fromStep,
  };

  try {
    const r = await fetch('/api/release/run', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!j.ok) { await showAlert(j.error || 'Falhou', {icon:'❌', title:'Erro ao iniciar'}); return; }
    activePipeline = 'release';
    setRunningUI(true);
    closeWizard();
    connectStream();
  } catch(e) {
    await showAlert('Erro de rede: ' + e.message, {icon:'❌', title:'Erro de rede'});
  } finally {
    btn.disabled = false;
    btn.textContent = isResume ? `▶ Retomar da etapa ${stepLabel}` : '▶ Confirmar e Iniciar';
  }
}

async function togglePause() {
  const paused = document.getElementById('btn-pause').textContent.includes('Continuar')
              || document.getElementById('btn-hotfix-pause').textContent.includes('Continuar');
  const endpoint = paused ? '/api/release/resume' : '/api/release/pause';
  await fetch(endpoint, {method:'POST'});
}

async function stopRelease() {
  const ok = await showConfirm('Parar a release em execução?', {icon:'⛔', title:'Parar release', okLabel:'Parar', okCls:'danger'});
  if (!ok) return;
  await fetch('/api/release/stop', {method:'POST'});
}

// ── utils (ansi) ──────────────────────────────────────────────────────────────
function ansiStrip(s) {
  return s.replace(/\x1B(?:[@-Z\\-_]|\[[0-9;]*[mGKHJF]|\[\?[0-9;]*[lh])/g,'');
}

// ── kanban ────────────────────────────────────────────────────────────────────
async function persistTasks() {
  await fetch('/api/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(tasks)});
  renderOverview();
  renderTime();
}

function updateFilterOptions() {
  const sprints = [...new Set(tasks.map(t=>t.sprint).filter(Boolean))].sort();
  const persons = [...new Set(tasks.map(t=>t.person).filter(Boolean))].sort();

  ['filter-sprint','time-sprint-filter','kpi-sprint-filter'].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    const cur = el.value;
    const placeholder = id === 'kpi-sprint-filter' ? 'Sprint atual' : 'Todas as sprints';
    el.innerHTML = `<option value="">${placeholder}</option>` +
      sprints.map(s=>`<option value="${s}" ${s===cur?'selected':''}>${s}</option>`).join('');
  });

  const peEl = document.getElementById('filter-person');
  const curPe = peEl.value;
  peEl.innerHTML = '<option value="">Todos</option>' +
    persons.map(p=>`<option value="${p}" ${p===curPe?'selected':''}>${p}</option>`).join('');
}

function getFilters() {
  return {
    sprint: document.getElementById('filter-sprint').value,
    person: document.getElementById('filter-person').value,
    type:   document.getElementById('filter-type').value,
    search: document.getElementById('filter-search').value.toLowerCase(),
  };
}

function renderKanban() {
  updateFilterOptions();
  const f = getFilters();
  const board = document.getElementById('kanban-board');

  const filtered = tasks.filter(t => {
    if (f.sprint && t.sprint !== f.sprint) return false;
    if (f.person && t.person !== f.person) return false;
    if (f.type   && t.type   !== f.type)   return false;
    if (f.search && !JSON.stringify(t).toLowerCase().includes(f.search)) return false;
    return true;
  });

  board.innerHTML = COLS.map(col => {
    const ct = filtered.filter(t=>t.status===col.id);
    return `<div class="kanban-col" id="col-${col.id}"
      ondragover="onDragOver(event,'${col.id}')"
      ondragleave="onDragLeave('${col.id}')"
      ondrop="onDrop(event,'${col.id}')">
      <div class="col-header">
        <span class="col-dot" style="background:${col.color}"></span>
        <span class="col-name">${col.name}</span>
        <span class="col-count">${ct.length}</span>
      </div>
      <div class="col-body">
        ${ct.map(t=>taskCardHTML(t)).join('')}
      </div>
      <button class="col-add" onclick="openTaskModal(null,'${col.id}')">+ Adicionar</button>
    </div>`;
  }).join('');
}

function taskCardHTML(t) {
  const typeMap = {feat:'Feature',fix:'Fix',chore:'Chore',other:'Outro'};
  const dt = t.date ? new Date(t.date).toLocaleDateString('pt-BR') : '';
  const due = t.due ? new Date(t.due) : null;
  const overdue = due && due < new Date() && t.status !== 'concluidas';
  return `<div class="task-card" draggable="true"
    ondragstart="onDragStart(event,'${t.id}')"
    onclick="onCardClick(event,'${t.id}')"
    id="card-${t.id}"
    ${overdue?'style="border-color:#DC2626"':''}>
    <div class="card-top">
      <span class="card-title">${esc(t.title)}</span>
    </div>
    <div class="card-meta">
      <span class="chip chip-type-${t.type||'other'}">${typeMap[t.type]||'Outro'}</span>
      <span class="chip chip-prio-${t.prio||'medium'}">${{high:'🔴 Alta',medium:'🟡 Média',low:'🟢 Baixa'}[t.prio]||'Média'}</span>
      ${t.jira?`<a class="chip chip-type-other" href="https://zanthus.atlassian.net/browse/${esc(t.jira)}" target="_blank" style="text-decoration:none;">${esc(t.jira)}</a>`:''}
      ${overdue?'<span class="chip" style="background:#DC262618;color:#DC2626;border-color:#DC262630">Atrasado</span>':''}
    </div>
    <div class="card-footer">
      ${t.person?`<span class="card-avatar">${initials(t.person)}</span>`:''}
      <span style="flex:1">${esc(t.sprint||'')}</span>
      <span>${dt}</span>
    </div>
  </div>`;
}

let _dragging = false;
function onDragStart(e, id) {
  _dragging = true;
  e.dataTransfer.setData('taskId', id);
  setTimeout(()=>{ _dragging = false; }, 300);
}
function onCardClick(e, id) {
  if (_dragging) return;
  openTaskModal(id);
}
function onDragOver(e,col){e.preventDefault();document.getElementById('col-'+col).classList.add('drag-over');}
function onDragLeave(col){document.getElementById('col-'+col).classList.remove('drag-over');}
function onDrop(e,col){
  e.preventDefault();
  document.getElementById('col-'+col).classList.remove('drag-over');
  const id=e.dataTransfer.getData('taskId');
  const t=tasks.find(t=>t.id===id);
  if(t){t.status=col;persistTasks();renderKanban();}
}

// ── time page ─────────────────────────────────────────────────────────────────
function renderTime() {
  updateFilterOptions();
  const spFilter = document.getElementById('time-sprint-filter').value;
  const filtered = spFilter ? tasks.filter(t=>t.sprint===spFilter) : tasks;
  const members  = [...new Set(filtered.map(t=>t.person).filter(Boolean))].sort();

  const grid = document.getElementById('member-grid');

  if (!members.length) {
    grid.innerHTML = `<div style="color:var(--text3);font-size:13px">
      Nenhum membro encontrado. Adicione tarefas com responsável preenchido.
    </div>`;
    return;
  }

  const statusMeta = {
    importantes:  {cls:'mc-blocked', label:'IMPORTANTES'},
    planejadas:   {cls:'mc-backlog',  label:'Planejadas'},
    solicitacoes: {cls:'mc-backlog',  label:'Solicitações'},
    andamento:    {cls:'mc-doing',    label:'Andamento'},
    aguardando:   {cls:'mc-review',   label:'Aguard. Auth'},
    validacao:    {cls:'mc-review',   label:'Em validação'},
    formalizar:   {cls:'mc-doing',    label:'P/ Formalizar'},
    formalizadas: {cls:'mc-done',     label:'Formalizadas'},
    etapas:       {cls:'mc-doing',    label:'Etapas Release'},
    concluidas:   {cls:'mc-done',     label:'Concluídas'},
  };

  grid.innerHTML = members.map(person => {
    const mt = filtered.filter(t=>t.person===person);
    const bySt = {};
    mt.forEach(t=>{ bySt[t.status] = (bySt[t.status]||0)+1; });

    const chips = Object.entries(bySt)
      .filter(([,n])=>n>0)
      .map(([st,n])=>{
        const m = statusMeta[st] || {cls:'mc-backlog',label:st};
        return `<span class="member-chip ${m.cls}">${n} ${m.label}</span>`;
      }).join('');

    const taskItems = mt.slice(0,6).map(t => {
      const col = COL_COLORS[t.status] || '#94A3B8';
      return `<div class="mt-item" onclick="openTaskModal('${t.id}')">
        <span class="mt-dot" style="background:${col}"></span>
        <span class="mt-title">${esc(t.title)}</span>
        <span class="mt-sprint">${esc(t.sprint||'')}</span>
      </div>`;
    }).join('');

    const extra = mt.length > 6 ? `<div class="member-empty">+ ${mt.length-6} mais</div>` : '';

    return `<div class="member-card">
      <div class="member-card-top">
        <div class="member-avatar">${initials(person)}</div>
        <div>
          <div class="member-name">${esc(person)}</div>
          <div class="member-stat">${mt.length} task${mt.length!==1?'s':''}</div>
          <div class="member-chips">${chips}</div>
        </div>
      </div>
      ${taskItems ? `<div class="member-tasks">${taskItems}${extra}</div>` : '<div class="member-empty">Sem tasks</div>'}
    </div>`;
  }).join('');
}

// ── KPI page ──────────────────────────────────────────────────────────────────

// ── comparativo entre releases (dois sprints lado a lado) ─────────────────────
function _releaseMetrics(sprint) {
  const st    = sprint ? tasks.filter(t => t.sprint === sprint) : [];
  const total = st.length;
  const bugs  = st.filter(t => t.type === 'fix').length;
  const done  = st.filter(t => t.status === 'concluidas').length;
  const mix   = total ? Math.round(bugs / total * 1000) / 10 : 0;
  const rate  = total ? Math.round(done / total * 1000) / 10 : 0;
  return {sprint, total, bugs, done, mix, rate};
}

function _populateReleaseCompareSelects() {
  const sprints = [...new Set(tasks.map(t=>t.sprint).filter(Boolean))].sort();
  const baseEl = document.getElementById('kpi-cmp-baseline');
  const curEl  = document.getElementById('kpi-cmp-current');
  if (!baseEl || !curEl) return;

  const prevBase = baseEl.value, prevCur = curEl.value;
  const opts = sprints.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('');
  baseEl.innerHTML = opts;
  curEl.innerHTML  = opts;

  if (sprints.length >= 2) {
    baseEl.value = sprints.indexOf(prevBase) >= 0 ? prevBase : sprints[sprints.length - 2];
    curEl.value  = sprints.indexOf(prevCur)  >= 0 ? prevCur  : sprints[sprints.length - 1];
  } else if (sprints.length === 1) {
    baseEl.value = sprints[0];
    curEl.value  = sprints[0];
  }
}

function _cmpDelta(curVal, baseVal, higherIsBetter) {
  const diff = Math.round((curVal - baseVal) * 10) / 10;
  if (diff === 0) return {text: '=', color: 'var(--text3)'};
  const improved = higherIsBetter ? diff > 0 : diff < 0;
  const color = improved ? 'var(--success)' : 'var(--danger)';
  const sign  = diff > 0 ? '+' : '';
  return {text: sign + diff, color};
}

function _evolutionNarrative(base, cur) {
  const parts = [];
  if (cur.total > base.total) parts.push('mais volume');
  else if (cur.total < base.total) parts.push('menos volume');
  if (cur.bugs < base.bugs) parts.push('menos bugs');
  else if (cur.bugs > base.bugs) parts.push('mais bugs');
  if (cur.rate > base.rate) parts.push('melhor entrega');
  else if (cur.rate < base.rate) parts.push('menor entrega');
  if (cur.mix < base.mix && !parts.includes('menos bugs')) parts.push('mix menor');
  return parts.length ? parts.join(' · ') : 'sem variação';
}

function renderReleaseComparison() {
  _populateReleaseCompareSelects();
  const baseEl = document.getElementById('kpi-cmp-baseline');
  const curEl  = document.getElementById('kpi-cmp-current');
  if (!baseEl || !curEl) return;

  const base = _releaseMetrics(baseEl.value);
  const cur  = _releaseMetrics(curEl.value);

  // Update hero tabs
  const baseNameEl  = document.getElementById('kpi-rtab-base-name');
  const curNameEl   = document.getElementById('kpi-rtab-cur-name');
  const narrativeEl = document.getElementById('kpi-rtab-narrative');
  if (baseNameEl)  baseNameEl.textContent  = base.sprint || '—';
  if (curNameEl)   curNameEl.textContent   = cur.sprint  || '—';
  if (narrativeEl) narrativeEl.textContent = (base.sprint && cur.sprint) ? _evolutionNarrative(base, cur) : '—';

  const cardsEl = document.getElementById('kpi-cmp-cards');
  const cardDefs = [
    {lbl:'Volume de atividades', baseVal: base.total, curVal: cur.total, unit:'', higherBetter:true,  accent:'var(--info)'},
    {lbl:'Bugs encontrados',     baseVal: base.bugs,  curVal: cur.bugs,  unit:'', higherBetter:false, accent:'var(--danger)'},
    {lbl:'Mix de bugs',          baseVal: base.mix,   curVal: cur.mix,   unit:'%', higherBetter:false, accent:'var(--success)'},
    {lbl:'Taxa de conclusão',    baseVal: base.rate,  curVal: cur.rate,  unit:'%', higherBetter:true,  accent:'var(--warn)'},
  ];
  cardsEl.innerHTML = cardDefs.map(c => {
    const delta = _cmpDelta(c.curVal, c.baseVal, c.higherBetter);
    return `<div class="kpi-cmp-card" style="border-top-color:${c.accent}">
      <div class="kpi-cmp-card-lbl">${c.lbl}</div>
      <div class="kpi-cmp-card-vals">
        <span class="kpi-cmp-card-before">${c.baseVal}${c.unit}</span>
        <span class="kpi-cmp-card-arrow">→</span>
        <span class="kpi-cmp-card-after">${c.curVal}${c.unit}</span>
      </div>
      <span class="kpi-cmp-card-delta" style="background:${delta.color}22;color:${delta.color}">${delta.text}${c.unit}</span>
    </div>`;
  }).join('');

  const chartsEl = document.getElementById('kpi-cmp-charts');
  const chartDefs = [
    {title:'Atividades',            baseVal: base.total, curVal: cur.total, color:'var(--info)'},
    {title:'Bugs',                  baseVal: base.bugs,  curVal: cur.bugs,  color:'#F43F5E', unit:''},
    {title:'Participação em bugs',  baseVal: base.mix,   curVal: cur.mix,   color:'var(--success)', unit:'%'},
    {title:'Conclusão',             baseVal: base.rate,  curVal: cur.rate,  color:'var(--warn)', unit:'%'},
  ];
  chartsEl.innerHTML = chartDefs.map(c => {
    const unit = c.unit || '';
    const maxV = Math.max(c.baseVal, c.curVal, 1);
    const hBase = Math.max(Math.round(c.baseVal / maxV * 100), c.baseVal > 0 ? 4 : 0);
    const hCur  = Math.max(Math.round(c.curVal  / maxV * 100), c.curVal  > 0 ? 4 : 0);
    return `<div class="kpi-cmp-chart-group">
      <div class="kpi-cmp-chart-title">${c.title}</div>
      <div class="kpi-cmp-bars">
        <div class="kpi-cmp-bar-col">
          <div class="kpi-cmp-bar-num">${c.baseVal}${unit}</div>
          <div class="kpi-cmp-bar" style="height:${hBase}%;background:var(--text3)"></div>
          <div class="kpi-cmp-bar-lbl">${esc(base.sprint || '—')}</div>
        </div>
        <div class="kpi-cmp-bar-col">
          <div class="kpi-cmp-bar-num">${c.curVal}${unit}</div>
          <div class="kpi-cmp-bar" style="height:${hCur}%;background:${c.color}"></div>
          <div class="kpi-cmp-bar-lbl">${esc(cur.sprint || '—')}</div>
        </div>
      </div>
    </div>`;
  }).join('');

  const insightsEl = document.getElementById('kpi-cmp-insights');
  const deltaRows = [
    {lbl:'Mix de bugs',       baseVal: base.mix  + '%', curVal: cur.mix  + '%', diff: Math.round((cur.mix  - base.mix) *10)/10, lowerBetter:true},
    {lbl:'Taxa de conclusão', baseVal: base.rate + '%', curVal: cur.rate + '%', diff: Math.round((cur.rate - base.rate)*10)/10, lowerBetter:false},
  ];
  const rowsHtml = deltaRows.map(r => {
    const improved = r.lowerBetter ? r.diff < 0 : r.diff > 0;
    const color = r.diff === 0 ? 'var(--text3)' : (improved ? 'var(--success)' : 'var(--danger)');
    const absDiff = Math.abs(r.diff);
    const subText = r.diff === 0 ? 'sem variação' : `${absDiff} pontos percentuais ${r.diff > 0 ? 'maior' : 'menor'}`;
    return `<div class="kpi-cmp-delta-row">
      <div class="kpi-cmp-delta-lbl">${r.lbl}</div>
      <div class="kpi-cmp-delta-vals">${r.baseVal} <span style="color:var(--text3)">→</span> <span style="color:${color}">${r.curVal}</span></div>
      <div class="kpi-cmp-delta-sub" style="color:${color}">${subText}</div>
    </div>`;
  }).join('');

  const insightsList = [];
  if (cur.total !== base.total) {
    insightsList.push(cur.total > base.total
      ? `↑ Maior volume entregue (${cur.total} vs ${base.total})`
      : `↓ Menor volume entregue (${cur.total} vs ${base.total})`);
  }
  if (cur.bugs !== base.bugs) {
    insightsList.push(cur.bugs < base.bugs
      ? `↓ Menos bugs encontrados (${cur.bugs} vs ${base.bugs})`
      : `↑ Mais bugs encontrados (${cur.bugs} vs ${base.bugs})`);
  }
  if (cur.mix !== base.mix) {
    insightsList.push(cur.mix < base.mix ? '↓ Menor participação de bugs' : '↑ Maior participação de bugs');
  }
  if (cur.rate !== base.rate) {
    insightsList.push(cur.rate >= base.rate ? '↑ Taxa de conclusão estável ou maior' : '↓ Taxa de conclusão menor');
  }

  const equationHtml = cur.sprint
    ? `<div class="kpi-cmp-equation"><b>${esc(cur.sprint)}</b>&nbsp;·&nbsp;${cur.total - cur.bugs} tarefas + ${cur.bugs} bugs = ${cur.total}</div>`
    : '';

  insightsEl.innerHTML = rowsHtml + equationHtml +
    (insightsList.length
      ? `<div class="kpi-cmp-insights-title">Principais insights</div>` +
        insightsList.map(i=>`<div class="kpi-cmp-insight">${esc(i)}</div>`).join('')
      : '');

  const summaryEl = document.getElementById('kpi-cmp-summary');
  const summaryLine = (m) => `<b>${esc(m.sprint)}</b>: ${m.total} atividades · ${m.bugs} bugs · ` +
    `${m.mix}% participação em bugs · ${m.done} tarefas concluídas · ${m.rate}%`;
  const rows = [base, cur].filter(m => m.sprint);
  summaryEl.innerHTML = rows.length
    ? rows.map(m => `<div class="kpi-cmp-summary-row">${summaryLine(m)}</div>`).join('')
    : `<div class="kpi-cmp-summary-row" style="color:var(--text3)">Cadastre tarefas com sprint preenchida em pelo menos duas releases para comparar.</div>`;
}

function renderKPIs() {
  updateFilterOptions();
  const spFilter  = document.getElementById('kpi-sprint-filter').value;
  const curSprint = cfg.current_sprint || '';
  const viewSprint = spFilter || curSprint;

  const sprintTasks = viewSprint ? tasks.filter(t => t.sprint === viewSprint) : tasks;
  const done        = sprintTasks.filter(t => t.status === 'concluidas');
  const hotfixes    = sprintTasks.filter(t => t.type === 'fix');
  const today       = new Date().toISOString().split('T')[0];
  const overdue     = sprintTasks.filter(t => t.due && t.due < today && t.status !== 'concluidas');
  const rate        = sprintTasks.length ? Math.round(done.length / sprintTasks.length * 100) : 0;

  // Carryover: tasks from sprints that sort before the viewed sprint, not concluidas
  const allSprints = [...new Set(tasks.map(t=>t.sprint).filter(Boolean))].sort();
  const spIdx      = viewSprint ? allSprints.indexOf(viewSprint) : allSprints.length;
  const prevSprints = allSprints.slice(0, spIdx);
  const carryover   = tasks.filter(t => prevSprints.includes(t.sprint) && t.status !== 'concluidas');

  document.getElementById('kpi-sprint-label').textContent = viewSprint ? `Exibindo: ${viewSprint}` : 'Sem sprint configurada';
  document.getElementById('kpi-total').textContent     = sprintTasks.length;
  document.getElementById('kpi-done').textContent      = done.length;
  document.getElementById('kpi-rate').textContent      = rate + '%';
  document.getElementById('kpi-carryover').textContent = carryover.length;
  document.getElementById('kpi-hotfixes').textContent  = hotfixes.length;
  document.getElementById('kpi-overdue').textContent   = overdue.length;

  _renderKpiDevs(sprintTasks, today);
  _renderKpiHistory(allSprints);
  _renderKpiTypes(sprintTasks);
}

function _renderKpiDevs(sprintTasks, today) {
  const members   = [...new Set(sprintTasks.map(t=>t.person).filter(Boolean))].sort();
  const container = document.getElementById('kpi-devs');
  if (!members.length) {
    container.innerHTML = `<div style="color:var(--text3);font-size:13px;padding:16px 0">Nenhum membro com tarefas nesta sprint.</div>`;
    return;
  }
  const typeColors = {feat:'var(--info)',fix:'var(--danger)',chore:'var(--warn)',other:'var(--text3)'};
  const typeLabels = {feat:'Feature',fix:'Fix',chore:'Chore',other:'Outro'};
  container.innerHTML = members.map(person => {
    const mt      = sprintTasks.filter(t => t.person === person);
    const doneCnt = mt.filter(t => t.status === 'concluidas').length;
    const ovd     = mt.filter(t => t.due && t.due < today && t.status !== 'concluidas').length;
    const r       = mt.length ? Math.round(doneCnt / mt.length * 100) : 0;
    const rCls    = r >= 80 ? 'kpi-rate-good' : r >= 50 ? 'kpi-rate-mid' : 'kpi-rate-low';
    const barClr  = r >= 80 ? 'var(--success)' : r >= 50 ? 'var(--warn)' : 'var(--danger)';
    const byType  = {feat:0,fix:0,chore:0,other:0};
    mt.forEach(t => { const k = t.type||'other'; byType[k]=(byType[k]||0)+1; });
    const chips = Object.entries(byType).filter(([,n])=>n>0).map(([tp,n]) =>
      `<span class="kpi-type-chip" style="background:${typeColors[tp]}22;color:${typeColors[tp]};border:1px solid ${typeColors[tp]}44">${typeLabels[tp]}: ${n}</span>`
    ).join('');
    const ovdHtml = ovd ? `· <span style="color:var(--danger)">${ovd} atrasada${ovd!==1?'s':''}</span>` : '';
    return `<div class="kpi-dev-card">
      <div class="kpi-dev-top">
        <div class="kpi-dev-avatar">${initials(person)}</div>
        <div class="kpi-dev-info">
          <div class="kpi-dev-name">${esc(person)}</div>
          <div class="kpi-dev-sub">${mt.length} task${mt.length!==1?'s':''} · ${doneCnt} entregue${doneCnt!==1?'s':''} ${ovdHtml}</div>
        </div>
        <div class="kpi-dev-rate ${rCls}">${r}%</div>
      </div>
      <div class="kpi-progress-bar"><div class="kpi-progress-fill" style="width:${r}%;background:${barClr}"></div></div>
      <div class="kpi-type-chips">${chips}</div>
    </div>`;
  }).join('');
}

function _renderKpiHistory(allSprints) {
  const container = document.getElementById('kpi-history');
  if (!allSprints.length) {
    container.innerHTML = `<div style="color:var(--text3);font-size:13px">Nenhum histórico disponível.</div>`;
    return;
  }
  container.innerHTML = [...allSprints].reverse().map(sprint => {
    const st   = tasks.filter(t => t.sprint === sprint);
    const d    = st.filter(t => t.status === 'concluidas').length;
    const r    = st.length ? Math.round(d / st.length * 100) : 0;
    const clr  = r >= 80 ? 'var(--success)' : r >= 50 ? 'var(--warn)' : 'var(--danger)';
    return `<div class="kpi-hist-row">
      <div class="kpi-hist-sprint" title="${esc(sprint)}">${esc(sprint)}</div>
      <div class="kpi-hist-bar-wrap"><div class="kpi-hist-bar-fill" style="width:${r}%;background:${clr}"></div></div>
      <div class="kpi-hist-nums">${d}/${st.length}</div>
      <div class="kpi-hist-rate" style="color:${clr}">${r}%</div>
    </div>`;
  }).join('');
}

function _renderKpiTypes(sprintTasks) {
  const container = document.getElementById('kpi-types');
  const total  = sprintTasks.length || 1;
  const counts = {feat:0,fix:0,chore:0,other:0};
  sprintTasks.forEach(t => { const k = t.type||'other'; counts[k]=(counts[k]||0)+1; });
  const typeColors = {feat:'#3B82F6',fix:'#EF4444',chore:'#F59E0B',other:'#94A3B8'};
  const typeLabels = {feat:'Feature',fix:'Fix / Hotfix',chore:'Chore',other:'Outro'};
  container.innerHTML = Object.entries(counts).map(([tp,n]) => {
    const pct = Math.round(n / total * 100);
    return `<div class="kpi-type-row">
      <div class="kpi-type-lbl">${typeLabels[tp]}</div>
      <div class="kpi-type-bar-wrap"><div class="kpi-type-bar-fill" style="width:${pct}%;background:${typeColors[tp]}"></div></div>
      <div class="kpi-type-cnt">${n}</div>
      <div class="kpi-type-pct">${pct}%</div>
    </div>`;
  }).join('');
}

// ── config modal ──────────────────────────────────────────────────────────────
function openCfgModal() {
  document.getElementById('m-cur-branch').value   = cfg.current_release || '';
  document.getElementById('m-version').value      = cfg.version         || '';
  document.getElementById('m-crm').value          = cfg.crm_version     || '';
  document.getElementById('m-sprint').value       = cfg.current_sprint  || '';
  document.getElementById('m-next-branch').value  = cfg.next_release    || '';
  document.getElementById('m-next-version').value = cfg.next_version    || '';
  document.getElementById('m-next-crm').value     = cfg.next_crm        || '';
  document.getElementById('m-registry').value     = cfg.docker_registry || '';
  document.getElementById('m-team').value         = cfg.team_name       || '';
  document.getElementById('m-slack-uid').value    = cfg.slack_user_id   || '';
  document.getElementById('cfg-modal').classList.add('open');
}
function closeCfgModal() { document.getElementById('cfg-modal').classList.remove('open'); }

async function saveCfgModal() {
  cfg = {
    current_release: document.getElementById('m-cur-branch').value.trim(),
    version:         document.getElementById('m-version').value.trim(),
    crm_version:     document.getElementById('m-crm').value.trim(),
    current_sprint:  document.getElementById('m-sprint').value.trim(),
    next_release:    document.getElementById('m-next-branch').value.trim(),
    next_version:    document.getElementById('m-next-version').value.trim(),
    next_crm:        document.getElementById('m-next-crm').value.trim(),
    docker_registry: document.getElementById('m-registry').value.trim(),
    team_name:       document.getElementById('m-team').value.trim(),
    slack_user_id:   document.getElementById('m-slack-uid').value.trim(),
  };
  await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(cfg)});
  renderAll();
  closeCfgModal();
}

// ── task card detail ──────────────────────────────────────────────────────────
let _editId  = null;
let _editCol = 'planejadas';

function _buildStatusSelect() {
  const sel = document.getElementById('t-status');
  sel.innerHTML = COLS.map(c => `<option value="${c.id}">${c.name}</option>`).join('');
}

function updateJiraLink() {
  const val = document.getElementById('t-jira').value.trim();
  const link = document.getElementById('t-jira-link');
  if (val) {
    link.style.display = 'block';
    link.href = `https://zanthus.atlassian.net/browse/${val}`;
    link.textContent = `Abrir ${val} no JIRA ↗`;
  } else {
    link.style.display = 'none';
  }
}

function openTaskModal(taskId, colId) {
  _editId  = taskId || null;
  _editCol = colId  || 'planejadas';

  _buildStatusSelect();

  const del = document.getElementById('task-del-btn');
  del.style.display = taskId ? 'inline-flex' : 'none';

  if (taskId) {
    const t = tasks.find(t=>t.id===taskId);
    if (!t) return;
    document.getElementById('task-id').value    = t.id;
    document.getElementById('t-title').value    = t.title   || '';
    document.getElementById('t-status').value   = t.status  || _editCol;
    document.getElementById('t-jira').value     = t.jira    || '';
    document.getElementById('t-type').value     = t.type    || 'feat';
    document.getElementById('t-prio').value     = t.prio    || 'medium';
    document.getElementById('t-sprint').value   = t.sprint  || '';
    document.getElementById('t-person').value   = t.person  || '';
    document.getElementById('t-date').value     = t.date    || '';
    document.getElementById('t-due').value      = t.due     || '';
    document.getElementById('t-branch').value   = t.branch  || '';
    document.getElementById('t-notes').value    = t.notes   || '';
  } else {
    ['task-id','t-title','t-jira','t-sprint','t-person','t-date','t-due','t-branch','t-notes'].forEach(id=>{
      document.getElementById(id).value = '';
    });
    document.getElementById('t-status').value = _editCol;
    document.getElementById('t-type').value   = 'feat';
    document.getElementById('t-prio').value   = 'medium';
    if (cfg.current_sprint) document.getElementById('t-sprint').value = cfg.current_sprint;
  }

  updateJiraLink();
  loadWorklogs();
  document.getElementById('task-modal').classList.add('open');
  setTimeout(()=>document.getElementById('t-title').focus(), 50);
}

// ── apontamento de horas (worklog) — lê/escreve direto no Jira ────────────────
async function loadWorklogs() {
  const jira     = document.getElementById('t-jira').value.trim();
  const totalEl  = document.getElementById('cd-worklog-total');
  const listEl   = document.getElementById('cd-worklog-list');
  const btn      = document.getElementById('cd-worklog-btn');
  const statusEl = document.getElementById('cd-worklog-status');

  statusEl.style.display = 'none';

  if (!jira) {
    totalEl.textContent = '—';
    listEl.innerHTML = '<div>Vincule um código do Jira para lançar/ver horas.</div>';
    btn.disabled = true;
    return;
  }

  btn.disabled = false;
  totalEl.textContent = '…';
  listEl.innerHTML = '';

  try {
    const r = await fetch(`/api/jira/worklogs/${encodeURIComponent(jira)}`);
    const j = await r.json();
    _renderWorklogResult(j);
  } catch (e) {
    totalEl.textContent = '—';
    listEl.innerHTML = `<div style="color:var(--danger)">Erro de rede: ${esc(e.message)}</div>`;
  }
}

function _renderWorklogResult(j) {
  const totalEl = document.getElementById('cd-worklog-total');
  const listEl  = document.getElementById('cd-worklog-list');

  if (!j.ok) {
    totalEl.textContent = '—';
    listEl.innerHTML = `<div style="color:var(--danger)">${esc(j.error || 'Falha ao buscar')}</div>`;
    return;
  }

  totalEl.textContent = j.total_display;
  listEl.innerHTML = j.entries.length
    ? j.entries.map(e => `<div style="padding:5px 0;border-bottom:1px solid var(--border);">
        <b style="color:var(--text2)">${esc(e.timeSpent)}</b> · ${esc(e.author)} · ${esc(e.started)}
        ${e.comment ? `<br>${esc(e.comment)}` : ''}
      </div>`).join('')
    : '<div>Nenhum apontamento ainda.</div>';
}

async function logWorklog() {
  const jira = document.getElementById('t-jira').value.trim();
  if (!jira) return;

  const timeSpent = document.getElementById('cd-worklog-time').value.trim();
  const comment   = document.getElementById('cd-worklog-comment').value.trim();
  const statusEl  = document.getElementById('cd-worklog-status');

  if (!timeSpent) {
    statusEl.style.display = 'block';
    statusEl.style.background = 'var(--danger)22';
    statusEl.style.color = 'var(--danger)';
    statusEl.textContent = 'Informe o tempo (ex: 2h, 30m, 1d).';
    return;
  }

  const btn = document.getElementById('cd-worklog-btn');
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = '⟳ Lançando…';

  try {
    const r = await fetch(`/api/jira/worklogs/${encodeURIComponent(jira)}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({timeSpent, comment}),
    });
    const j = await r.json();
    statusEl.style.display = 'block';

    if (!j.ok) {
      statusEl.style.background = 'var(--danger)22';
      statusEl.style.color = 'var(--danger)';
      statusEl.textContent = '✗ ' + (j.error || 'Falhou');
      return;
    }

    statusEl.style.background = 'var(--success)22';
    statusEl.style.color = 'var(--success)';
    statusEl.textContent = '✓ Horas lançadas no Jira!';
    document.getElementById('cd-worklog-time').value = '';
    document.getElementById('cd-worklog-comment').value = '';
    _renderWorklogResult(j);
  } catch (e) {
    statusEl.style.display = 'block';
    statusEl.style.background = 'var(--danger)22';
    statusEl.style.color = 'var(--danger)';
    statusEl.textContent = '✗ Erro de rede: ' + e.message;
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

function closeTaskModal() { document.getElementById('task-modal').classList.remove('open'); }

async function saveTask() {
  const title = document.getElementById('t-title').value.trim();
  if (!title) { document.getElementById('t-title').focus(); return; }

  const t = {
    id:     _editId || crypto.randomUUID(),
    status: document.getElementById('t-status').value || _editCol,
    title,
    jira:   document.getElementById('t-jira').value.trim(),
    type:   document.getElementById('t-type').value,
    prio:   document.getElementById('t-prio').value,
    sprint: document.getElementById('t-sprint').value.trim(),
    person: document.getElementById('t-person').value.trim(),
    date:   document.getElementById('t-date').value,
    due:    document.getElementById('t-due').value,
    branch: document.getElementById('t-branch').value.trim(),
    notes:  document.getElementById('t-notes').value.trim(),
  };

  if (_editId) {
    const i = tasks.findIndex(x=>x.id===_editId);
    if (i>=0) tasks[i]=t; else tasks.push(t);
  } else {
    tasks.push(t);
  }

  await persistTasks();
  closeTaskModal();
  renderKanban();
}

async function deleteTask() {
  if (!_editId) return;
  const ok = await showConfirm('Excluir esta atividade?', {icon:'🗑️', title:'Excluir atividade', okLabel:'Excluir', okCls:'danger'});
  if (!ok) return;
  tasks = tasks.filter(t=>t.id!==_editId);
  await persistTasks();
  closeTaskModal();
  renderKanban();
}

// ── utils ─────────────────────────────────────────────────────────────────────
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function initials(name){if(!name)return'?';return name.split(' ').map(w=>w[0]).join('').toUpperCase().slice(0,2);}

document.getElementById('wizard-modal').addEventListener('click',e=>{if(e.target===e.currentTarget)closeWizard();});
document.getElementById('cfg-modal').addEventListener('click',e=>{if(e.target===e.currentTarget)closeCfgModal();});
document.getElementById('task-modal').addEventListener('click',e=>{if(e.target===e.currentTarget)closeTaskModal();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeWizard();closeCfgModal();closeTaskModal();}});


init();

// ── Formalizar page ────────────────────────────────────────────────────────────

let _fzItems = [];
let _fzReplySpec = '';
let _fzReplyTimer = null;

async function renderFormalizar() {
  // Load reply spec
  const rs = await fetch('/api/formalizacoes/reply-spec');
  const rsj = await rs.json();
  _fzReplySpec = rsj.reply_spec || '';
  document.getElementById('fz-reply-spec').value = _fzReplySpec;

  const r = await fetch('/api/formalizacoes');
  const j = await r.json();
  _fzItems = j.items || [];
  _fzRenderList();
}

function fzOnReplySpecChange() {
  _fzReplySpec = document.getElementById('fz-reply-spec').value;
  const hint = document.getElementById('fz-reply-hint');
  hint.textContent = 'Salvando…';
  clearTimeout(_fzReplyTimer);
  _fzReplyTimer = setTimeout(async () => {
    await fetch('/api/formalizacoes/reply-spec', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({reply_spec: _fzReplySpec}),
    });
    hint.textContent = 'Salvo ✓';
    setTimeout(() => { hint.textContent = ''; }, 1500);
  }, 600);
}

function fzToggleBulk() {
  const box = document.getElementById('fz-bulk-box');
  box.style.display = box.style.display === 'none' ? '' : 'none';
}

async function fzBulkAdd() {
  const raw = document.getElementById('fz-bulk-tickets').value;
  const tickets = raw.split(/[\n,;]+/).map(t => t.trim()).filter(Boolean);
  if (!tickets.length) { await showAlert('Cole ao menos um ticket.', {icon:'⚠️', title:'Vazio'}); return; }
  const btn = document.getElementById('fz-bulk-btn');
  btn.disabled = true;
  btn.textContent = `Buscando ${tickets.length} ticket(s)…`;
  try {
    const r = await fetch('/api/formalizacoes/bulk', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tickets}),
    });
    const j = await r.json();
    if (!j.ok) { await showAlert(j.error || 'Erro', {icon:'❌', title:'Erro'}); return; }
    _fzItems.push(...j.added);
    _fzRenderList();
    document.getElementById('fz-bulk-tickets').value = '';
    document.getElementById('fz-bulk-box').style.display = 'none';
  } catch(e) {
    await showAlert('Erro de rede: ' + e.message, {icon:'❌', title:'Erro'});
  } finally {
    btn.disabled = false;
    btn.textContent = 'Buscar e adicionar todas';
  }
}

function _fzSlackPreview(item) {
  const tipo = (item.branch.startsWith('hotfix/') || item.branch.startsWith('fix/')) ? 'Hotfix' : 'Feature';
  const lines = [tipo, `Branch:  ${item.branch || '(sem branch)'}`, `Conteúdo: ${item.conteudo}`, `Task: ${item.task}`];
  if (item.subtask) lines.push(`Subtask: ${item.subtask}`);
  return lines.join('\n');
}

function _fzRenderList() {
  const el = document.getElementById('fz-list');
  if (!_fzItems.length) {
    el.innerHTML = `<div class="fz-empty">Nenhuma atividade adicionada ainda.<br>Use o formulário acima para adicionar.</div>`;
    return;
  }
  el.innerHTML = _fzItems.map(item => {
    const tipo = (item.branch.startsWith('hotfix/') || item.branch.startsWith('fix/')) ? 'Hotfix' : 'Feature';
    const badgeCls = {pending:'fz-badge-pending', sent:'fz-badge-sent', error:'fz-badge-error', sending:'fz-badge-sending', duplicate:'fz-badge-duplicate'}[item.status] || 'fz-badge-pending';
    const statusLabel = {pending:'Pendente', sent:'Enviado', error:'Erro', sending:'Enviando…', duplicate:'Já formalizada'}[item.status] || item.status;
    const canSend = item.status !== 'sent' && item.status !== 'sending';
    // items 'duplicate' podem ser reenviados via botão individual (com confirmação)
    const preview = esc(_fzSlackPreview(item));
    const replyPreview = esc(_fzReplySpec || 'CRM:\nRelease —\nFrontend:\n—');
    return `<div class="fz-item" id="fzi-${item.id}">
      <div class="fz-item-left">
        <div class="fz-item-top">
          <span class="fz-badge ${badgeCls}">${statusLabel}</span>
          <span class="fz-badge fz-badge-type">${tipo}</span>
          <span class="fz-task">${esc(item.task)}${item.subtask ? ' · ' + esc(item.subtask) : ''}</span>
        </div>
        <div class="fz-branch">${esc(item.branch || '⚠ branch não definida')}</div>
        <div class="fz-conteudo">${esc(item.conteudo)}</div>
        ${item.error ? `<div class="fz-item-error">❌ ${esc(item.error)}</div>` : ''}
        <details class="fz-preview">
          <summary>Prévia no Slack</summary>
          <pre class="fz-preview-msg">${preview}</pre>
          <div class="fz-preview-sep">↩ resposta na thread</div>
          <pre class="fz-preview-reply">${replyPreview}</pre>
        </details>
      </div>
      <div class="fz-item-actions">
        ${canSend ? `<button class="fz-btn fz-btn-send" onclick="fzSendItem('${item.id}')">↗ Enviar</button>` : ''}
        ${item.slack_ts ? `<button class="fz-btn fz-btn-del-slack" onclick="fzDeleteSlack('${item.id}')" title="Apagar mensagem do Slack">🗑 Slack</button>` : ''}
        <button class="fz-btn fz-btn-edit" onclick="fzEditItem('${item.id}')">✎ Editar</button>
        <button class="fz-btn fz-btn-del" onclick="fzDeleteItem('${item.id}')">✕</button>
      </div>
    </div>`;
  }).join('');
}

function _fzAutoConteudo(ticket, branch) {
  const base = branch.replace(/^(hotfix|fix|feature|feat)\//i, '').replace(/^[A-Za-z0-9]+-\d+-?/, '');
  const clean = base.replace(/-v\d+\.\d+.*$/, '');
  const parts = clean.split('-').filter(Boolean);
  if (!parts.length) return `#${ticket}# - `;
  const module = parts[0].charAt(0).toUpperCase() + parts[0].slice(1);
  const desc   = parts.slice(1).map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
  return `#${ticket}# - ${module}${desc ? ' - ' + desc : ''}`;
}

function fzOnTicketInput() {
  // Clear branch results when ticket changes
  const sel = document.getElementById('fz-branch-sel');
  sel.innerHTML = '<option value="">— busque ou digite abaixo —</option>';
}

async function fzSearchBranches() {
  const ticket = document.getElementById('fz-ticket').value.trim().toUpperCase();
  if (!ticket) { await showAlert('Digite o ticket antes de buscar.', {icon:'⚠️', title:'Campo obrigatório'}); return; }
  const btn = document.getElementById('fz-search-btn');
  btn.textContent = 'Buscando…';
  btn.disabled = true;
  try {
    const r = await fetch('/api/git/branches?ticket=' + encodeURIComponent(ticket));
    const j = await r.json();
    const sel = document.getElementById('fz-branch-sel');
    if (!j.ok || !j.branches.length) {
      sel.innerHTML = '<option value="">Nenhuma branch encontrada — use o campo manual</option>';
      return;
    }
    sel.innerHTML = '<option value="">Selecione…</option>' +
      j.branches.map(b => `<option value="${esc(b)}">${esc(b)}</option>`).join('');
  } catch(e) {
    await showAlert('Erro ao buscar: ' + e.message, {icon:'❌', title:'Erro'});
  } finally {
    btn.textContent = 'Buscar no git';
    btn.disabled = false;
  }
}

function fzOnBranchSel() {
  const sel  = document.getElementById('fz-branch-sel');
  const branch = sel.value;
  if (!branch) return;
  document.getElementById('fz-branch-manual').value = '';
  const ticket = document.getElementById('fz-ticket').value.trim().toUpperCase();
  if (ticket && !document.getElementById('fz-conteudo').value.trim()) {
    document.getElementById('fz-conteudo').value = _fzAutoConteudo(ticket, branch);
  }
}

function fzOnManualBranch() {
  // If user types manually, clear the select
  const manual = document.getElementById('fz-branch-manual').value.trim();
  if (manual) {
    document.getElementById('fz-branch-sel').value = '';
    const ticket = document.getElementById('fz-ticket').value.trim().toUpperCase();
    if (ticket && !document.getElementById('fz-conteudo').value.trim()) {
      document.getElementById('fz-conteudo').value = _fzAutoConteudo(ticket, manual);
    }
  }
}

function _fzGetBranch() {
  const manual = document.getElementById('fz-branch-manual').value.trim();
  const sel    = document.getElementById('fz-branch-sel').value;
  return manual || sel;
}

async function fzAddItem() {
  const task     = document.getElementById('fz-ticket').value.trim().toUpperCase();
  const branch   = _fzGetBranch();
  const conteudo = document.getElementById('fz-conteudo').value.trim();
  const subtask  = document.getElementById('fz-subtask').value.trim().toUpperCase();

  if (!task)     { await showAlert('Informe o ticket (ex: NEOSDEV-6519)', {icon:'⚠️', title:'Campo obrigatório'}); return; }
  if (!branch)   { await showAlert('Informe ou busque a branch', {icon:'⚠️', title:'Campo obrigatório'}); return; }
  if (!conteudo) { await showAlert('Preencha o Conteúdo', {icon:'⚠️', title:'Campo obrigatório'}); return; }

  const r = await fetch('/api/formalizacoes', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ task, subtask, branch, conteudo }),
  });
  const j = await r.json();
  if (!j.ok) { await showAlert(j.error || 'Erro', {icon:'❌', title:'Erro'}); return; }

  _fzItems.push(j.item);
  _fzRenderList();

  // Reset form
  document.getElementById('fz-ticket').value  = '';
  document.getElementById('fz-conteudo').value = '';
  document.getElementById('fz-subtask').value  = '';
  document.getElementById('fz-branch-manual').value = '';
  document.getElementById('fz-branch-sel').innerHTML = '<option value="">— busque ou digite abaixo —</option>';
}

async function fzSendItem(id, force = false, silent = false) {
  const item = _fzItems.find(i => i.id === id);
  if (item) item.status = 'sending';
  _fzRenderList();

  const r = await fetch(`/api/formalizacoes/${id}/enviar`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ force }),
  });
  const j = await r.json();
  const idx = _fzItems.findIndex(i => i.id === id);

  if (r.status === 409 && j.duplicate) {
    if (silent) {
      // Modo batch: marca na lista como duplicata e continua sem modal
      if (_fzItems[idx]) {
        _fzItems[idx].status = 'duplicate';
        _fzItems[idx].error = j.posted_at ? `Já formalizada em ${j.posted_at}` : 'Já formalizada no Slack';
      }
      _fzRenderList();
      return;
    }
    // Modo individual: pergunta se quer forçar
    if (_fzItems[idx]) { _fzItems[idx].status = 'pending'; }
    _fzRenderList();
    const when = j.posted_at ? ` (postado em ${j.posted_at})` : '';
    const ok = await showWarningConfirm(
      `Esta branch já foi formalizada no Slack${when}.\n\nDeseja enviar mesmo assim?`,
      {title:'Branch já formalizada', okLabel:'Enviar mesmo assim'}
    );
    if (ok) await fzSendItem(id, true, false);
    return;
  }

  if (j.ok && j.item) {
    _fzItems[idx] = j.item;
  } else if (!j.ok) {
    if (_fzItems[idx]) { _fzItems[idx].status = 'error'; _fzItems[idx].error = j.error || 'Falhou'; }
  }
  _fzRenderList();
}

async function fzDeleteItem(id) {
  await fetch('/api/formalizacoes/' + id, { method: 'DELETE' });
  _fzItems = _fzItems.filter(i => i.id !== id);
  _fzRenderList();
}

async function fzDeleteSlack(id) {
  const ok = await showWarningConfirm(
    'Apagar esta mensagem do Slack?\n\nO item voltará ao status Pendente.',
    {title: 'Apagar do Slack', okLabel: 'Apagar'}
  );
  if (!ok) return;
  const r = await fetch(`/api/formalizacoes/${id}/deletar-slack`, { method: 'POST' });
  const j = await r.json();
  if (j.ok && j.item) {
    const idx = _fzItems.findIndex(i => i.id === id);
    if (idx >= 0) _fzItems[idx] = j.item;
  } else {
    await showAlert('Erro ao apagar: ' + (j.error || 'falha desconhecida'));
  }
  _fzRenderList();
}

async function fzEditItem(id) {
  const item = _fzItems.find(i => i.id === id);
  if (!item) return;
  const result = await showFzEditModal(item);
  if (!result) return;

  const r = await fetch('/api/formalizacoes/' + id, {
    method: 'PUT',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify(result),
  });
  const j = await r.json();
  if (j.ok && j.item) {
    const idx = _fzItems.findIndex(i => i.id === id);
    _fzItems[idx] = j.item;
    _fzRenderList();
  }
}

async function fzSendAll() {
  const pending = _fzItems.filter(i => i.status === 'pending' || i.status === 'error' || i.status === 'duplicate');
  if (!pending.length) { showAlert('Nenhum item pendente.'); return; }
  const ok = await showConfirm(`Enviar ${pending.length} item(s) para o Slack?`);
  if (!ok) return;
  for (const item of pending) {
    await fzSendItem(item.id, false, true);  // silent=true: duplicatas marcadas na lista, sem modal
    await new Promise(r => setTimeout(r, 800));
  }
}

// ── modal helpers ─────────────────────────────────────────────────────────────
(function() {
  const backdrop = document.getElementById('app-modal');
  const iconEl   = document.getElementById('modal-icon');
  const titleEl  = document.getElementById('modal-title');
  const msgEl    = document.getElementById('modal-msg');
  const actionsEl= document.getElementById('modal-actions');
  let _resolve   = null;

  function _open(icon, title, msg, buttons) {
    iconEl.textContent  = icon;
    titleEl.textContent = title;
    msgEl.textContent   = msg;
    actionsEl.innerHTML = '';
    buttons.forEach(({label, cls, value}) => {
      const btn = document.createElement('button');
      btn.className   = 'modal-btn ' + (cls || '');
      btn.textContent = label;
      btn.onclick     = () => { _close(); if (_resolve) _resolve(value); };
      actionsEl.appendChild(btn);
    });
    backdrop.classList.add('open');
    // fechar com Esc
    backdrop._onkey = e => { if (e.key === 'Escape') { _close(); if (_resolve) _resolve(false); } };
    document.addEventListener('keydown', backdrop._onkey);
  }

  function _close() {
    backdrop.classList.remove('open');
    document.removeEventListener('keydown', backdrop._onkey);
  }

  window.showAlert = function(msg, {icon='ℹ️', title='Aviso'} = {}) {
    return new Promise(res => {
      _resolve = res;
      _open(icon, title, msg, [{label:'OK', cls:'primary', value:true}]);
    });
  };

  window.showConfirm = function(msg, {icon='❓', title='Confirmação', okLabel='Confirmar', okCls='primary', cancelLabel='Cancelar'} = {}) {
    return new Promise(res => {
      _resolve = res;
      _open(icon, title, msg, [
        {label: cancelLabel, cls: '',    value: false},
        {label: okLabel,     cls: okCls, value: true},
      ]);
    });
  };

  window.showWarningConfirm = function(msg, {title='Atenção', okLabel='Enviar mesmo assim', cancelLabel='Cancelar'} = {}) {
    return showConfirm(msg, {icon:'⚠️', title, okLabel, okCls:'danger', cancelLabel});
  };

  window.showFzEditModal = function(item) {
    return new Promise(res => {
      _resolve = null;
      iconEl.textContent  = '✎';
      titleEl.textContent = 'Editar item';
      const esc = s => (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
      msgEl.innerHTML = `
        <div class="modal-field"><label>Branch</label><input id="fz-ed-branch" type="text" value="${esc(item.branch)}"></div>
        <div class="modal-field"><label>Conteúdo</label><input id="fz-ed-conteudo" type="text" value="${esc(item.conteudo)}"></div>
        <div class="modal-field"><label>Subtask</label><input id="fz-ed-subtask" type="text" placeholder="ex: NEOSDEV-6521" value="${esc(item.subtask||'')}"></div>
      `;
      actionsEl.innerHTML = '';
      const cancel = document.createElement('button');
      cancel.className   = 'modal-btn';
      cancel.textContent = 'Cancelar';
      cancel.onclick = () => { _close(); res(null); };
      const ok = document.createElement('button');
      ok.className   = 'modal-btn primary';
      ok.textContent = 'Salvar';
      ok.onclick = () => {
        const result = {
          branch:   document.getElementById('fz-ed-branch').value,
          conteudo: document.getElementById('fz-ed-conteudo').value,
          subtask:  document.getElementById('fz-ed-subtask').value,
        };
        _close();
        res(result);
      };
      actionsEl.appendChild(cancel);
      actionsEl.appendChild(ok);
      backdrop.classList.add('open');
      backdrop._onkey = e => {
        if (e.key === 'Escape') { _close(); res(null); }
        if (e.key === 'Enter' && e.target && e.target.id !== 'fz-ed-subtask') { ok.click(); }
      };
      document.addEventListener('keydown', backdrop._onkey);
      setTimeout(() => {
        const inp = document.getElementById('fz-ed-branch');
        if (inp) { inp.focus(); inp.select(); }
      }, 50);
    });
  };
})();
</script>
</body>
</html>"""

if __name__ == "__main__":
    url = "http://localhost:5050"
    print(f"\n  ZCRM Release Dashboard")
    print(f"  → {url}\n")
    threading.Timer(1.3, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=5050, debug=False, threaded=True)
