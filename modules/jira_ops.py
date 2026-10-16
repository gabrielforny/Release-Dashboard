"""
Jira integration — busca issues do Jira Cloud via REST API v3 e mapeia para o
formato de task usado pelo kanban "Esteira do Time" do dashboard.

Requer, no ambiente (.env):
  JIRA_BASE_URL   ex: https://zanthus.atlassian.net
  JIRA_EMAIL      e-mail da conta usada para gerar o token
  JIRA_TOKEN      API token gerado em https://id.atlassian.com/manage-profile/security/api-tokens

A autenticação usa Basic Auth (email + token), que é o método padrão do
Jira Cloud para tokens de API pessoais (diferente de Jira Server/Data Center).
"""

import os
import re
from typing import List, Optional

import requests

_TIMEOUT = 15


class JiraError(Exception):
    pass


def _config() -> dict:
    return {
        "base_url": os.getenv("JIRA_BASE_URL", "").rstrip("/"),
        "email": os.getenv("JIRA_EMAIL", ""),
        "token": os.getenv("JIRA_TOKEN", ""),
    }


def is_configured() -> bool:
    cfg = _config()
    return bool(cfg["base_url"] and cfg["email"] and cfg["token"])


def _auth(cfg: dict):
    return (cfg["email"], cfg["token"])


def test_connection() -> dict:
    """
    Verifica se as credenciais estão corretas chamando /myself.
    Retorna {"ok": True, "user": "Nome"} ou lança JiraError com a causa.
    """
    cfg = _config()
    if not is_configured():
        raise JiraError(
            "Configuração incompleta — defina JIRA_BASE_URL, JIRA_EMAIL e JIRA_TOKEN no .env"
        )
    try:
        resp = requests.get(
            f"{cfg['base_url']}/rest/api/3/myself",
            auth=_auth(cfg),
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise JiraError(f"Falha de conexão com {cfg['base_url']}: {e}") from e

    if resp.status_code == 401:
        raise JiraError("Autenticação falhou — verifique JIRA_EMAIL e JIRA_TOKEN")
    if not resp.ok:
        raise JiraError(f"Jira retornou {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    return {"ok": True, "user": data.get("displayName", cfg["email"])}


_sprint_field_id_cache: Optional[str] = None


def _discover_sprint_field_id(cfg: dict) -> Optional[str]:
    """
    O campo "Sprint" no Jira é um custom field cujo ID (customfield_XXXXX)
    varia por instância — não existe um ID fixo. Descobre dinamicamente
    procurando pelo campo cujo nome é "Sprint" em /rest/api/3/field.
    """
    global _sprint_field_id_cache
    if _sprint_field_id_cache is not None:
        return _sprint_field_id_cache

    try:
        resp = requests.get(
            f"{cfg['base_url']}/rest/api/3/field", auth=_auth(cfg), timeout=_TIMEOUT
        )
        resp.raise_for_status()
        for field in resp.json():
            if field.get("name", "").lower() == "sprint":
                _sprint_field_id_cache = field["id"]
                return _sprint_field_id_cache
    except requests.RequestException:
        pass

    _sprint_field_id_cache = ""
    return None


def _extract_sprint_name(issue_fields: dict, sprint_field_id: Optional[str]) -> str:
    if not sprint_field_id:
        return ""
    raw = issue_fields.get(sprint_field_id)
    if not raw:
        return ""
    # A API Agile retorna uma lista de objetos {name, state, ...} (ou strings
    # legadas "com.atlassian.greenhopper...[name=Sprint 56,...]" em instâncias antigas).
    try:
        last = raw[-1] if isinstance(raw, list) else raw
        if isinstance(last, dict):
            return last.get("name", "")
        if isinstance(last, str) and "name=" in last:
            part = last.split("name=")[1]
            return part.split(",")[0].strip()
    except (IndexError, KeyError, TypeError):
        pass
    return ""


# Mapeamento padrão: nome do tipo de issue do Jira → type do kanban
DEFAULT_TYPE_MAP = {
    "bug": "fix",
    "erro": "fix",
    "story": "feat",
    "estoria": "feat",
    "história": "feat",
    "task": "feat",
    "tarefa": "feat",
    "subtarefa": "chore",
    "sub-task": "chore",
    "melhoria": "chore",
    "improvement": "chore",
}

# Mapeamento padrão: nome do status do Jira → coluna do kanban
# Inclui os status reais do workflow "Neos - Releases" (Para Release, Para
# Validação, Em Qualidade, Concluído), além dos nomes genéricos mais comuns.
DEFAULT_STATUS_MAP = {
    "to do": "planejadas",
    "a fazer": "planejadas",
    "backlog": "planejadas",
    "in progress": "andamento",
    "em andamento": "andamento",
    "in review": "aguardando",
    "em revisão": "aguardando",
    "code review": "aguardando",
    "para release": "aguardando",
    "qa": "validacao",
    "em validação": "validacao",
    "em qualidade": "validacao",
    "para validação": "validacao",
    "em teste": "validacao",
    "done": "concluidas",
    "concluído": "concluidas",
    "concluída": "concluidas",
    "closed": "concluidas",
    "fechado": "concluidas",
}


def _extract_fix_version(fields: dict) -> str:
    """
    fixVersions pode conter mais de um item (ex: um bug corrigido retroativamente
    em "Neosense 55, Neosense 56"). Para comparar release a release, usa a versão
    mais recente (maior número "Neosense NN"), não simplesmente a primeira da lista.
    """
    versions = fields.get("fixVersions") or []
    if not versions or not isinstance(versions, list):
        return ""

    names = [v.get("name", "") for v in versions if v.get("name")]
    if not names:
        return ""

    def _version_number(name: str) -> float:
        m = re.search(r"(\d+)", name)
        return int(m.group(1)) if m else -1

    return max(names, key=_version_number)


def map_issue_to_task(
    issue: dict,
    sprint_field_id: Optional[str],
    type_map: Optional[dict] = None,
    status_map: Optional[dict] = None,
) -> dict:
    fields = issue.get("fields", {})
    type_map = {**DEFAULT_TYPE_MAP, **(type_map or {})}
    status_map = {**DEFAULT_STATUS_MAP, **(status_map or {})}

    issuetype_name = (fields.get("issuetype") or {}).get("name", "").lower()
    status_name = (fields.get("status") or {}).get("name", "").lower()
    assignee = fields.get("assignee") or {}
    priority_name = (fields.get("priority") or {}).get("name", "").lower()

    prio = "medium"
    if "high" in priority_name or "alta" in priority_name or "highest" in priority_name:
        prio = "high"
    elif "low" in priority_name or "baixa" in priority_name:
        prio = "low"

    # Nesse board (tickets de release), o campo "Sprint" do Jira Agile costuma
    # vir vazio — o que de fato indica a release é "Versões corrigidas"
    # (fixVersion, ex: "Neosense 56"). Usa o Sprint quando existir; senão cai
    # para a fixVersion, que é o eixo real de comparação "release a release".
    sprint = _extract_sprint_name(fields, sprint_field_id) or _extract_fix_version(fields)

    return {
        "id": f"jira-{issue['key']}",
        "title": fields.get("summary", issue["key"]),
        "type": type_map.get(issuetype_name, "other"),
        "status": status_map.get(status_name, "planejadas"),
        "person": assignee.get("displayName", ""),
        "sprint": sprint,
        "jira": issue["key"],
        "prio": prio,
        "date": (fields.get("created") or "")[:10],
        "due": (fields.get("duedate") or ""),
    }


def fetch_issues(jql: str, max_results: int = 200) -> List[dict]:
    """
    Busca issues no Jira via JQL e retorna já mapeadas para o formato de task.

    Usa /rest/api/3/search/jql (POST) — o endpoint /rest/api/3/search clássico
    foi desativado pela Atlassian em 2025. A paginação desse endpoint novo é
    feita por 'nextPageToken' em vez do antigo 'startAt'/'total'.
    """
    cfg = _config()
    if not is_configured():
        raise JiraError(
            "Configuração incompleta — defina JIRA_BASE_URL, JIRA_EMAIL e JIRA_TOKEN no .env"
        )

    sprint_field_id = _discover_sprint_field_id(cfg)

    issues: List[dict] = []
    next_token: Optional[str] = None
    fields = ["summary", "status", "issuetype", "assignee", "priority", "created", "duedate", "fixVersions"]
    if sprint_field_id:
        fields.append(sprint_field_id)

    while len(issues) < max_results:
        body = {
            "jql": jql,
            "maxResults": min(100, max_results - len(issues)),
            "fields": fields,
        }
        if next_token:
            body["nextPageToken"] = next_token

        try:
            resp = requests.post(
                f"{cfg['base_url']}/rest/api/3/search/jql",
                auth=_auth(cfg),
                json=body,
                timeout=_TIMEOUT,
            )
        except requests.RequestException as e:
            raise JiraError(f"Falha de conexão com o Jira: {e}") from e

        if resp.status_code == 400:
            raise JiraError(f"JQL inválida: {resp.json().get('errorMessages', resp.text)}")
        if resp.status_code == 401:
            raise JiraError("Autenticação falhou — verifique JIRA_EMAIL e JIRA_TOKEN")
        if not resp.ok:
            raise JiraError(f"Jira retornou {resp.status_code}: {resp.text[:300]}")

        data = resp.json()
        batch = data.get("issues", [])
        issues.extend(batch)

        next_token = data.get("nextPageToken")
        if not batch or data.get("isLast", not next_token) or not next_token:
            break

    return [map_issue_to_task(i, sprint_field_id) for i in issues[:max_results]]


def fetch_issue_info(issue_key: str) -> dict:
    """
    Busca título e subtask de Frontend de uma issue.
    Retorna {"title": str, "subtask": str}.
    Não lança exceção — retorna vazios em caso de falha.
    """
    cfg = _config()
    if not is_configured():
        return {"title": "", "subtask": ""}

    try:
        resp = requests.get(
            f"{cfg['base_url']}/rest/api/3/issue/{issue_key}",
            auth=_auth(cfg),
            params={"fields": "summary,subtasks"},
            timeout=_TIMEOUT,
        )
        if not resp.ok:
            return {"title": "", "subtask": ""}

        data = resp.json()
        fields = data.get("fields", {})
        title = fields.get("summary", "")

        subtask_key = ""
        for st in fields.get("subtasks", []):
            st_summary = (st.get("fields", {}).get("summary", "") or "").lower()
            if any(kw in st_summary for kw in ["frontend", "front-end", "front end", "front "]):
                subtask_key = st["key"]
                break

        return {"title": title, "subtask": subtask_key}
    except Exception:
        return {"title": "", "subtask": ""}


# ---------------------------------------------------------------------------
# Worklogs — apontamento de horas (equivalente ao "Team Reports" do Jira)
# ---------------------------------------------------------------------------


def _format_seconds(total_seconds: int) -> str:
    """60 -> '1m' | 3600 -> '1h' | 28800 -> '1d' | 5400 -> '1.5h'"""
    if total_seconds <= 0:
        return "0h"
    if total_seconds < 3600:
        return f"{int(total_seconds / 60)}m"
    hours = total_seconds / 3600
    if hours >= 8 and hours % 8 == 0:
        return f"{int(hours / 8)}d"
    if hours == int(hours):
        return f"{int(hours)}h"
    return f"{hours:.1f}h"


def _adf_to_text(comment) -> str:
    """Extrai texto puro de um 'comment' em Atlassian Document Format (ou string simples)."""
    if not comment:
        return ""
    if isinstance(comment, str):
        return comment

    texts: List[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                texts.append(node.get("text", ""))
            for child in node.get("content", []) or []:
                walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(comment)
    return " ".join(t for t in texts if t).strip()


def _text_to_adf(text: str) -> dict:
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}] if text else [],
    }


def get_worklogs(issue_key: str, max_results: int = 20) -> dict:
    """
    Retorna os apontamentos de horas de uma issue: total (em segundos e já
    formatado, ex: '3.5h') e as últimas entradas (autor, tempo, data, comentário).
    """
    cfg = _config()
    if not is_configured():
        raise JiraError(
            "Configuração incompleta — defina JIRA_BASE_URL, JIRA_EMAIL e JIRA_TOKEN no .env"
        )

    try:
        resp = requests.get(
            f"{cfg['base_url']}/rest/api/3/issue/{issue_key}/worklog",
            auth=_auth(cfg),
            params={"maxResults": 1000},
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise JiraError(f"Falha de conexão com o Jira: {e}") from e

    if resp.status_code == 404:
        raise JiraError(f"Issue {issue_key} não encontrada no Jira")
    if resp.status_code == 401:
        raise JiraError("Autenticação falhou — verifique JIRA_EMAIL e JIRA_TOKEN")
    if not resp.ok:
        raise JiraError(f"Jira retornou {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    worklogs = data.get("worklogs", [])
    total_seconds = sum(w.get("timeSpentSeconds", 0) for w in worklogs)
    ordered = sorted(worklogs, key=lambda w: w.get("started", ""), reverse=True)

    entries = [
        {
            "id": w.get("id", ""),
            "author": (w.get("author") or {}).get("displayName", ""),
            "timeSpent": w.get("timeSpent", ""),
            "started": (w.get("started") or "")[:10],
            "comment": _adf_to_text(w.get("comment")),
        }
        for w in ordered[:max_results]
    ]

    return {
        "total_seconds": total_seconds,
        "total_display": _format_seconds(total_seconds),
        "entries": entries,
    }


def add_worklog(issue_key: str, time_spent: str, comment: str = "") -> dict:
    """
    Lança um apontamento de horas na issue. 'time_spent' usa o formato de
    duração do próprio Jira (ex: "2h", "30m", "1d 4h") — o Jira valida e
    converte internamente de acordo com as configurações de calendário da
    instância (horas por dia / dias por semana).
    """
    cfg = _config()
    if not is_configured():
        raise JiraError(
            "Configuração incompleta — defina JIRA_BASE_URL, JIRA_EMAIL e JIRA_TOKEN no .env"
        )

    body: dict = {"timeSpent": time_spent}
    if comment:
        body["comment"] = _text_to_adf(comment)

    try:
        resp = requests.post(
            f"{cfg['base_url']}/rest/api/3/issue/{issue_key}/worklog",
            auth=_auth(cfg),
            json=body,
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise JiraError(f"Falha de conexão com o Jira: {e}") from e

    if resp.status_code == 400:
        raise JiraError(
            f"Formato de tempo inválido — use algo como '2h', '30m' ou '1d'. "
            f"Detalhe: {resp.json().get('errorMessages', resp.text)}"
        )
    if resp.status_code == 404:
        raise JiraError(f"Issue {issue_key} não encontrada no Jira")
    if resp.status_code == 401:
        raise JiraError("Autenticação falhou — verifique JIRA_EMAIL e JIRA_TOKEN")
    if not resp.ok:
        raise JiraError(f"Jira retornou {resp.status_code}: {resp.text[:300]}")

    return resp.json()


def delete_worklog(issue_key: str, worklog_id: str) -> None:
    """Remove um apontamento de horas (usado principalmente para corrigir lançamentos)."""
    cfg = _config()
    if not is_configured():
        raise JiraError(
            "Configuração incompleta — defina JIRA_BASE_URL, JIRA_EMAIL e JIRA_TOKEN no .env"
        )
    try:
        resp = requests.delete(
            f"{cfg['base_url']}/rest/api/3/issue/{issue_key}/worklog/{worklog_id}",
            auth=_auth(cfg),
            timeout=_TIMEOUT,
        )
    except requests.RequestException as e:
        raise JiraError(f"Falha de conexão com o Jira: {e}") from e

    if resp.status_code == 404:
        raise JiraError("Apontamento não encontrado (já pode ter sido removido)")
    if resp.status_code == 401:
        raise JiraError("Autenticação falhou — verifique JIRA_EMAIL e JIRA_TOKEN")
    if not resp.ok:
        raise JiraError(f"Jira retornou {resp.status_code}: {resp.text[:300]}")
