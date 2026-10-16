"""
Release context — shared state passed between all steps.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class StepResult:
    name: str
    status: StepStatus = StepStatus.PENDING
    duration: float = 0.0
    output: str = ""
    error: str = ""


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------

_VERSION_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)_zcrm_api_\d+\.\d+\.\d+")


def version_from_branch(branch: str) -> str:
    """release/v2.51.0_zcrm_api_2.51.0_RC  →  v2.51.0_zcrm_api_2.51.0"""
    return branch.removeprefix("release/").removesuffix("_RC")


def increment_version(version: str) -> str:
    """
    v2.51.0_zcrm_api_2.51.0  →  v2.52.0_zcrm_api_2.52.0
    Increments the minor part of both version segments.
    """
    m = _VERSION_RE.match(version)
    if not m:
        return version
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    next_minor = minor + 1
    return f"v{major}.{next_minor}.{patch}_zcrm_api_{major}.{next_minor}.{patch}"


def increment_crm_version(crm_version: str) -> str:
    """53.0  →  54.0"""
    try:
        major, *rest = crm_version.split(".")
        return f"{int(major) + 1}.{'.'.join(rest) if rest else '0'}"
    except (ValueError, AttributeError):
        return crm_version


def branch_from_version(version: str) -> str:
    """v2.52.0_zcrm_api_2.52.0  →  release/v2.52.0_zcrm_api_2.52.0_RC"""
    return f"release/{version}_RC"


# Versão completa, capturando também o patch do segmento zcrm_api (hotfixes
# normalmente não mexem no crmVersion, mas o padrão de tag sempre tem os 3 números).
_FULL_VERSION_RE = re.compile(r"v(\d+)\.(\d+)\.(\d+)_zcrm_api_(\d+)\.(\d+)\.(\d+)")


def parse_full_version(version: str) -> Optional[tuple]:
    """
    v2.30.1_zcrm_api_2.30.0  →  (2, 30, 1, 2, 30, 0)
    Retorna None se a string não casar com o padrão esperado.
    """
    m = _FULL_VERSION_RE.match(version)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def increment_patch_version(version: str) -> str:
    """
    v2.30.0_zcrm_api_2.30.0  →  v2.30.1_zcrm_api_2.30.0
    Incrementa apenas o patch do frontend (número de hotfix), mantendo o
    crmVersion inalterado — é o padrão usado para versionar hotfixes.
    """
    parts = parse_full_version(version)
    if not parts:
        return version
    fmaj, fmin, fpatch, cmaj, cmin, cpatch = parts
    return f"v{fmaj}.{fmin}.{fpatch + 1}_zcrm_api_{cmaj}.{cmin}.{cpatch}"


def release_branch_for_version(version: str) -> str:
    """
    v2.30.4_zcrm_api_2.30.0  →  release/v2.30.0_zcrm_api_2.30.0_RC
    Deriva a branch de release "base" (patch zerado) a partir de qualquer
    tag de hotfix ou da própria tag de fechamento da release.
    """
    parts = parse_full_version(version)
    if not parts:
        return ""
    fmaj, fmin, _, cmaj, cmin, _ = parts
    return f"release/v{fmaj}.{fmin}.0_zcrm_api_{cmaj}.{cmin}.0_RC"


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class ReleaseContext:
    # --- Current release ---
    current_release: str = ""       # release/v2.51.0_zcrm_api_2.51.0_RC
    frontend_version: str = ""      # v2.51.0_zcrm_api_2.51.0  (lido do package.json ou branch)
    crm_version: str = ""           # 53.0  (lido do package.json)

    # --- Next release ---
    next_release: str = ""          # release/v2.52.0_zcrm_api_2.52.0_RC
    next_crm_version: str = ""      # 54.0  (incrementado automaticamente)

    # --- Branches ---
    # Quando vêm do Slack: lista de dicts {branch, ticket, description}
    # Quando entradas manualmente: apenas branch_names é preenchido
    branch_entries: List[dict] = field(default_factory=list)
    branch_names: List[str] = field(default_factory=list)   # entrada manual

    docker_registry: str = "zanthusinovacao1/zcrm_front"

    @property
    def branches(self) -> List[str]:
        """Lista de nomes de branch independente da origem."""
        if self.branch_entries:
            return [e["branch"] for e in self.branch_entries]
        return self.branch_names

    # --- Action flags ---
    do_close_release: bool = True
    do_create_next: bool = True
    do_generate_image: bool = True
    do_publish: bool = True

    # --- Runtime state ---
    start_time: datetime = field(default_factory=datetime.now)
    step_results: List[StepResult] = field(default_factory=list)
    merged_branches: List[str] = field(default_factory=list)
    failed_branches: List[str] = field(default_factory=list)
    changelog_entries: List[dict] = field(default_factory=list)
    build_log_path: Optional[str] = None
    dev_branch: str = ""     # dev/vX.Y.Z — preenchido pelo step_16

    # --- Derived properties ---

    @property
    def next_version(self) -> str:
        """release/v2.52.0_zcrm_api_2.52.0_RC  →  v2.52.0_zcrm_api_2.52.0"""
        return version_from_branch(self.next_release)

    @property
    def docker_tag(self) -> str:
        return f"{self.docker_registry}:{self.frontend_version}"

    @property
    def docker_tag_rc(self) -> str:
        return f"{self.docker_registry}:{self.next_version}_RC"

    def record(self, result: StepResult) -> None:
        self.step_results.append(result)


# ---------------------------------------------------------------------------
# Hotfix context (ver seção "Geração de hotfix" do processo de release)
# ---------------------------------------------------------------------------


@dataclass
class HotfixContext:
    # --- O que compõe o hotfix ---
    hotfix_branch: str = ""         # hotfix/NEOSDEV-1234-corrige-login
    ticket: str = ""                # NEOSDEV-1234
    description: str = ""           # descrição para o CHANGELOG

    # --- Onde o hotfix entra primeiro ---
    target_release_branch: str = ""  # release/v2.30.0_zcrm_api_2.30.0_RC
    hotfix_version: str = ""         # v2.30.4_zcrm_api_2.30.0 (versão de patch)

    # --- Propagação (quando o hotfix é de uma release já fechada/anterior) ---
    is_previous_release: bool = False
    current_release_branch: str = ""  # release/v2.31.0_zcrm_api_2.31.0_RC

    docker_registry: str = "zanthusinovacao1/zcrm_front"

    # --- Runtime state ---
    start_time: datetime = field(default_factory=datetime.now)
    step_results: List[StepResult] = field(default_factory=list)
    build_log_path: Optional[str] = None

    @property
    def docker_tag(self) -> str:
        return f"{self.docker_registry}:{self.hotfix_version}"

    def record(self, result: StepResult) -> None:
        self.step_results.append(result)
