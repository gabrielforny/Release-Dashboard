"""
Slack integration — busca branches aprovadas para a release.

Lógica:
  1. Percorre mensagens do #team-frontend
  2. Identifica formalizações (mensagens com "Branch:" e "Conteúdo:")
  3. Para cada formalização, lê as respostas (thread replies)
  4. Se alguma resposta contiver o padrão de aprovação da release atual
     (ex: "Release 53.0" + "v2.51.0_zcrm_api_2.51.0"), a branch entra
  5. Extrai Branch e Conteúdo (já formatado para o changelog)

Requer: SLACK_TOKEN no ambiente
"""

import os
import re
from datetime import datetime
from typing import List, Optional


def ts_to_str(ts: str) -> str:
    """Converte um timestamp Slack (epoch) para data/hora legível (dd/mm/aaaa hh:mm:ss)."""
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return ts or ""


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

#   - "Branch:" / "Conteúdo:" / "Task:"  (forma singular, 1 item por linha)
#   - "Branch(es):" / "Conteúdo(s):" / "Task(s):"  (forma plural, itens em
#     lista com bullets "•" abaixo do label)
_PLURAL = r"(?:\(s\)|\(es\))?"
_NEXT_FIELD = r"(?:Branch|Conteúdo|Task|Subtask)"  # próximo label conhecido

_BRANCH_RE  = re.compile(rf"\*?Branch{_PLURAL}\*?:\*?\s*(\S+)", re.IGNORECASE)
_CONTENT_BLOCK_RE = re.compile(
    rf"\*?Conteúdo{_PLURAL}\*?:\*?\s*(.*?)(?=\n\*?{_NEXT_FIELD}{_PLURAL}\*?:|$)",
    re.IGNORECASE | re.DOTALL,
)
# (?<!sub) evita confundir "Task(s):" com a cauda de "Subtask(s):"
_TASK_BLOCK_RE = re.compile(
    rf"(?<!sub)\*?Task{_PLURAL}\*?:\*?\s*(.*?)(?=\n\*?{_NEXT_FIELD}{_PLURAL}\*?:|$)",
    re.IGNORECASE | re.DOTALL,
)
_TICKET_PREFIX_RE = re.compile(r"^#[\w-]+#\s*-?\s*", re.IGNORECASE)
_TICKET_TOKEN_RE = re.compile(r"[A-Z][A-Z0-9]+-\d+", re.IGNORECASE)
_SLACK_LINK_RE = re.compile(r"<(?:([^|>]+)\|)?([^>]*)>")
_BULLET_PREFIX_RE = re.compile(r"^[•\-\*]\s*")


def _strip_slack_links(text: str) -> str:
    """Converte markup de link do Slack (<url|texto> ou <url>) em texto puro."""
    def repl(m: "re.Match") -> str:
        url, label = m.group(1), m.group(2)
        return label if label else (url or "")
    return _SLACK_LINK_RE.sub(repl, text)


def _split_block_items(block: str) -> list[str]:
    """
    Divide um bloco de campo (Conteúdo/Task) em itens individuais.
    Suporta tanto uma única linha (forma singular) quanto várias linhas
    com bullets "•" (forma plural).
    """
    items = []
    for line in block.strip().splitlines():
        line = _BULLET_PREFIX_RE.sub("", line.strip()).strip()
        if line:
            items.append(line)
    return items


def _extract_ticket(branch: str, text: str, raw_content: str) -> str:
    """
    Extrai o ticket (ex: NEOSDEV-5688) com a seguinte prioridade:
      1. Direto do nome da branch (mais confiável — sempre corresponde ao
         item real, mesmo quando o campo "Task(s):" do Slack vem com
         múltiplos links concatenados, ex: link vazio para o Épico + link
         do ticket, ou múltiplos tickets quando a formalização agrupa
         vários itens)
      2. Último ticket mencionado no bloco "Task(s):" (Slack costuma
         colocar o Épico/projeto primeiro e o(s) ticket(s) reais depois)
      3. Padrão #NEOSDEV-XXXX# dentro do próprio Conteúdo
    """
    branch_m = _TICKET_TOKEN_RE.search(branch)
    if branch_m:
        return branch_m.group(0).upper()

    task_block_m = _TASK_BLOCK_RE.search(text)
    if task_block_m:
        tokens = _TICKET_TOKEN_RE.findall(task_block_m.group(1))
        if tokens:
            return tokens[-1].upper()

    t = re.search(r"#([\w-]+)#", raw_content)
    if t:
        return t.group(1)

    return ""


def _parse_formalization(text: str) -> Optional[dict]:
    """
    Extrai Branch, Conteúdo e Task de uma mensagem de formalização.
    Suporta tanto a forma singular ("Branch:", "Conteúdo:", "Task:") quanto
    a plural com lista de bullets ("Branch(es):", "Conteúdo(s):", "Task(s):").
    Retorna None se a mensagem não for uma formalização.
    """
    branch_m  = _BRANCH_RE.search(text)
    content_m = _CONTENT_BLOCK_RE.search(text)

    if not branch_m or not content_m:
        return None

    branch = branch_m.group(1).strip()
    content_block = content_m.group(1)
    items = _split_block_items(content_block)
    if not items:
        return None

    cleaned_items = []
    for item in items:
        # Remove prefixo #NEOSDEV-785# - deixando só a descrição
        item = _TICKET_PREFIX_RE.sub("", item).strip()
        # Remove markup de link do Slack (<url|texto> → texto)
        item = _strip_slack_links(item).strip()
        if item:
            cleaned_items.append(item)
    description = "; ".join(cleaned_items)

    ticket = _extract_ticket(branch, text, content_block)

    return {
        "branch":      branch,
        "description": description,
        "ticket":      ticket,
    }


def _is_approval_reply(text: str, crm_version: str, frontend_version: str) -> bool:
    """
    Verifica se o texto de uma resposta é a aprovação para esta release.

    Padrão esperado:
        CRM:
        Release 53.0
        Frontend:
        v2.51.0_zcrm_api_2.51.0
    """
    has_release  = bool(re.search(
        rf"Release\s+{re.escape(crm_version)}", text, re.IGNORECASE
    ))
    has_frontend = frontend_version in text
    return has_release and has_frontend


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class SlackError(Exception):
    """Erro com mensagem legível para exibir na UI."""


# Diagnóstico da última chamada a fetch_approved_branches
last_fetch_diag: dict = {}


def fetch_approved_branches(
    crm_version: str,
    frontend_version: str,
    channel: str = "",
    limit: int = 200,
) -> List[dict]:
    """
    Retorna lista de dicts aprovados para a release.
    Lança SlackError com mensagem legível em caso de falha.
    """
    token = os.getenv("SLACK_TOKEN")
    if not token:
        raise SlackError("SLACK_TOKEN não definido no ambiente")

    if not channel:
        channel = os.getenv("SLACK_CHANNEL", "team-frontend")

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        raise SlackError("Pacote slack_sdk não instalado — execute: pip install slack_sdk")

    client = WebClient(token=token)

    # --- Resolve channel name → ID ---
    channel_id, resolve_error = _resolve_channel(client, channel)
    if not channel_id:
        raise SlackError(
            f"Canal '{channel}' não encontrado.\n"
            f"  Verifique:\n"
            f"  1. O bot foi convidado para #{channel} com /invite @ZCRM Release Bot\n"
            f"  2. O token tem o scope 'channels:read' ou 'groups:read'\n"
            + (f"  Detalhe do erro: {resolve_error}" if resolve_error else "")
        )

    # --- Percorre mensagens do canal ---
    approved: list[dict] = []
    seen_branches: set[str] = set()
    total_msgs = 0
    total_formalizations = 0

    try:
        cursor = None
        while True:
            kwargs = {"channel": channel_id, "limit": limit}
            if cursor:
                kwargs["cursor"] = cursor

            try:
                history = client.conversations_history(**kwargs)
            except SlackApiError as e:
                raise SlackError(
                    f"Erro ao ler histórico do canal #{channel}: {e.response['error']}\n"
                    f"  Verifique se o bot tem o scope 'channels:history' (canais públicos) "
                    f"ou 'groups:history' (canais privados)."
                )

            messages = history.get("messages", [])
            total_msgs += len(messages)

            for msg in messages:
                text = msg.get("text", "")
                ts   = msg.get("ts", "")

                entry = _parse_formalization(text)
                if not entry:
                    continue

                entry["formalized_at"] = ts
                total_formalizations += 1

                if not msg.get("reply_count", 0):
                    continue

                try:
                    replies = client.conversations_replies(
                        channel=channel_id, ts=ts
                    ).get("messages", [])
                except SlackApiError:
                    continue

                # A primeira mensagem da thread é a própria — pula ela
                for reply in replies[1:]:
                    reply_text = reply.get("text", "")
                    if _is_approval_reply(reply_text, crm_version, frontend_version):
                        branch = entry["branch"]
                        if branch not in seen_branches:
                            seen_branches.add(branch)
                            approved.append(entry)
                        break

            meta = history.get("response_metadata", {})
            cursor = meta.get("next_cursor")
            if not cursor:
                break

    except SlackError:
        raise
    except Exception as e:
        raise SlackError(f"Erro inesperado ao buscar branches: {e}")

    global last_fetch_diag
    last_fetch_diag = {
        "total_msgs": total_msgs,
        "total_formalizations": total_formalizations,
        "crm_version": crm_version,
        "frontend_version": frontend_version,
    }

    return approved


def _resolve_channel(client, name: str):
    """Converte nome do canal em ID. Retorna (id, error_msg)."""
    clean = name.lstrip("#")
    try:
        cursor = None
        while True:
            kwargs = {"limit": 200, "types": "public_channel,private_channel"}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.conversations_list(**kwargs)
            for ch in resp.get("channels", []):
                if ch.get("name") == clean:
                    return ch["id"], None
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        return None, str(e)
    return None, None


# ---------------------------------------------------------------------------
# Posting helpers — formalização de branches no canal
# ---------------------------------------------------------------------------


def post_formalization(
    branch: str,
    conteudo: str,
    task: str,
    subtask: str = "",
    channel: str = "",
) -> str:
    """
    Posta uma formalização no canal Slack.
    Retorna o timestamp (ts) da mensagem postada, usado para responder na thread.
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.getenv("SLACK_TOKEN")
    if not token:
        raise SlackError("SLACK_TOKEN não definido no ambiente")

    client = WebClient(token=token)
    ch = channel or os.getenv("SLACK_CHANNEL", "team-frontend")
    channel_id, err = _resolve_channel(client, ch)
    if not channel_id:
        raise SlackError(f"Canal '{ch}' não encontrado: {err}")

    tipo = "Hotfix" if branch.startswith(("hotfix/", "fix/")) else "Feature"

    lines = [tipo, f"Branch:  {branch}", f"Conteúdo: {conteudo}", f"Task: {task}"]
    if subtask:
        lines.append(f"Subtask: {subtask}")

    try:
        resp = client.chat_postMessage(channel=channel_id, text="\n".join(lines))
        return resp["ts"]
    except SlackApiError as e:
        raise SlackError(f"Erro ao postar formalização: {e.response['error']}")


def check_already_formalized(
    branch: str,
    channel: str = "",
    limit: int = 200,
) -> dict:
    """
    Verifica se uma branch já foi formalizada no canal Slack.
    Retorna {"formalized": bool, "ts": str | None, "posted_at": str | None}
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.getenv("SLACK_TOKEN")
    if not token:
        raise SlackError("SLACK_TOKEN não definido no ambiente")

    client = WebClient(token=token)
    ch = channel or os.getenv("SLACK_CHANNEL", "team-frontend")
    channel_id, err = _resolve_channel(client, ch)
    if not channel_id:
        raise SlackError(f"Canal '{ch}' não encontrado: {err}")

    try:
        cursor = None
        while True:
            kwargs = {"channel": channel_id, "limit": limit}
            if cursor:
                kwargs["cursor"] = cursor
            try:
                history = client.conversations_history(**kwargs)
            except SlackApiError as e:
                raise SlackError(f"Erro ao ler histórico: {e.response['error']}")

            for msg in history.get("messages", []):
                entry = _parse_formalization(msg.get("text", ""))
                if entry and entry["branch"] == branch:
                    return {
                        "formalized": True,
                        "ts": msg.get("ts"),
                        "posted_at": ts_to_str(msg.get("ts", "")),
                    }

            cursor = history.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except SlackError:
        raise
    except Exception as e:
        raise SlackError(f"Erro inesperado ao verificar formalização: {e}")

    return {"formalized": False, "ts": None, "posted_at": None}


def reply_text(
    thread_ts: str,
    text: str,
    channel: str = "",
) -> str:
    """Responde a uma thread do Slack com texto livre. Retorna o ts da resposta."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.getenv("SLACK_TOKEN")
    if not token:
        raise SlackError("SLACK_TOKEN não definido no ambiente")

    client = WebClient(token=token)
    ch = channel or os.getenv("SLACK_CHANNEL", "team-frontend")
    channel_id, err = _resolve_channel(client, ch)
    if not channel_id:
        raise SlackError(f"Canal '{ch}' não encontrado: {err}")

    try:
        resp = client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)
        return resp["ts"]
    except SlackApiError as e:
        raise SlackError(f"Erro ao responder thread: {e.response['error']}")


def delete_message(
    ts: str,
    channel: str = "",
) -> None:
    """Apaga uma mensagem postada pelo bot (requer chat:write)."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.getenv("SLACK_TOKEN")
    if not token:
        raise SlackError("SLACK_TOKEN não definido no ambiente")

    client = WebClient(token=token)
    ch = channel or os.getenv("SLACK_CHANNEL", "team-frontend")
    channel_id, err = _resolve_channel(client, ch)
    if not channel_id:
        raise SlackError(f"Canal '{ch}' não encontrado: {err}")

    try:
        client.chat_delete(channel=channel_id, ts=ts)
    except SlackApiError as e:
        raise SlackError(f"Erro ao deletar mensagem: {e.response['error']}")


def post_plain_message(
    text: str,
    channel: str = "",
) -> str:
    """Posta uma mensagem de texto livre no canal. Retorna o ts da mensagem."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.getenv("SLACK_TOKEN")
    if not token:
        raise SlackError("SLACK_TOKEN não definido no ambiente")

    client = WebClient(token=token)
    ch = channel or os.getenv("SLACK_CHANNEL", "team-frontend")
    channel_id, err = _resolve_channel(client, ch)
    if not channel_id:
        raise SlackError(f"Canal '{ch}' não encontrado: {err}")

    try:
        resp = client.chat_postMessage(channel=channel_id, text=text)
        return resp["ts"]
    except SlackApiError as e:
        raise SlackError(f"Erro ao postar mensagem: {e.response['error']}")


def send_dm(user_id: str, text: str) -> str:
    """
    Envia mensagem direta (DM) para um usuário do Slack.
    user_id: ID do membro no Slack (ex: U01ABC123) — não é o @nome.
    Retorna o ts da mensagem enviada.
    """
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.getenv("SLACK_TOKEN")
    if not token:
        raise SlackError("SLACK_TOKEN não definido no ambiente")

    client = WebClient(token=token)
    try:
        dm = client.conversations_open(users=[user_id])
        channel_id = dm["channel"]["id"]
        resp = client.chat_postMessage(channel=channel_id, text=text)
        return resp["ts"]
    except SlackApiError as e:
        raise SlackError(f"Erro ao enviar DM: {e.response['error']}")


def reply_release_spec(
    thread_ts: str,
    crm_release: str,
    frontend_version: str,
    channel: str = "",
) -> None:
    """Responde a thread de formalização com a spec da release."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    token = os.getenv("SLACK_TOKEN")
    if not token:
        raise SlackError("SLACK_TOKEN não definido no ambiente")

    client = WebClient(token=token)
    ch = channel or os.getenv("SLACK_CHANNEL", "team-frontend")
    channel_id, err = _resolve_channel(client, ch)
    if not channel_id:
        raise SlackError(f"Canal '{ch}' não encontrado: {err}")

    text = f"CRM:\nRelease {crm_release}\nFrontend:\n{frontend_version}"
    try:
        client.chat_postMessage(channel=channel_id, thread_ts=thread_ts, text=text)
    except SlackApiError as e:
        raise SlackError(f"Erro ao responder thread: {e.response['error']}")
