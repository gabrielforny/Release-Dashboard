"""
Changelog generation — parses branch names and produces CHANGELOG entries.
"""

import re
from datetime import datetime
from pathlib import Path
from typing import List, Optional


_TICKET_RE = re.compile(r"(NEOSDEV-\d+)", re.IGNORECASE)
_JIRA_BASE_URL = "https://zanthus.atlassian.net/browse"


def extract_ticket(branch: str) -> Optional[str]:
    m = _TICKET_RE.search(branch)
    return m.group(1).upper() if m else None


def branch_to_description(branch: str) -> str:
    """
    feature/NEOSDEV-5800-correcao-upload-csv
    →  Correcao upload csv
    """
    name = branch.split("/")[-1]
    # Remove ticket prefix
    name = _TICKET_RE.sub("", name).strip("-").strip("_").strip()
    # Replace separators
    name = name.replace("-", " ").replace("_", " ").strip()
    return name.capitalize() if name else branch


def build_entries(branches: List[str]) -> List[dict]:
    entries = []
    for branch in branches:
        ticket = extract_ticket(branch)
        description = branch_to_description(branch)
        entries.append(
            {
                "branch": branch,
                "ticket": ticket or branch,
                "description": description,
            }
        )
    return entries


def render_block(version: str, entries: List[dict]) -> str:
    date_str = datetime.now().strftime("%d/%m/%Y")
    lines = [f"## {version} ({date_str})"]

    features = [e for e in entries if e["branch"].startswith("feature/")]
    hotfixes = [e for e in entries if not e["branch"].startswith("feature/")]

    for label, group in (("Features", features), ("Hotfixes", hotfixes)):
        if not group:
            continue
        lines.append(f"- **[{label}]**")
        for e in group:
            ticket = e["ticket"]
            lines.append(
                f"    - [{e['description']} [{ticket}]]({_JIRA_BASE_URL}/{ticket})"
            )

    return "\n" + "\n".join(lines) + "\n"


def build_hotfix_entry(branch: str, ticket: str = "", description: str = "") -> dict:
    """Monta a entrada de changelog de um hotfix, com fallback para extração automática."""
    return {
        "branch": branch,
        "ticket": ticket or extract_ticket(branch) or branch,
        "description": description or branch_to_description(branch),
    }


def render_hotfix_block(version: str, entry: dict) -> str:
    """
    Bloco de changelog para uma única entrada de hotfix — mesmo formato de
    render_block(), mas sempre sob o rótulo "Hotfix" (conforme o processo:
    "O changelog deve ser alterado contendo a chave do JIRA e o conteúdo do hotfix").
    """
    date_str = datetime.now().strftime("%d/%m/%Y")
    ticket = entry["ticket"]
    lines = [
        f"## {version} ({date_str})",
        "- **[Hotfix]**",
        f"    - [{entry['description']} [{ticket}]]({_JIRA_BASE_URL}/{ticket})",
    ]
    return "\n" + "\n".join(lines) + "\n"


def prepend_to_changelog(changelog_path: Path, block: str) -> None:
    existing = ""
    if changelog_path.exists():
        existing = changelog_path.read_text(encoding="utf-8")

    # Keep the header line if present (e.g. "# Changelog")
    if existing.startswith("# "):
        header_end = existing.index("\n") + 1
        header = existing[:header_end]
        rest = existing[header_end:]
        new_content = header + block + rest
    else:
        new_content = block + existing

    changelog_path.write_text(new_content, encoding="utf-8")
