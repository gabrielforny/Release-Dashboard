#!/usr/bin/env bash
# Wrapper shell para a ferramenta de automação de hotfix ZCRM.
# Garante que o venv está ativo e delega para hotfix.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

# Criar venv se não existir ou se estiver corrompido (sem activate)
if [[ ! -f "$VENV/bin/activate" ]]; then
    [[ -d "$VENV" ]] && rm -rf "$VENV"
    echo "Criando ambiente virtual Python …"
    python3 -m venv "$VENV"
fi

# A partir daqui, usar sempre o python do venv
PYTHON="$VENV/bin/python3"

# Ativar venv
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# Instalar dependências se necessário
if ! "$PYTHON" -c "import rich" 2>/dev/null; then
    echo "Instalando dependências …"
    "$VENV/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi

# Carregar .env se existir
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -o allexport
    source "$SCRIPT_DIR/.env"
    set +o allexport
fi

# Executar a partir do diretório atual (para auto-detectar o repo)
exec "$PYTHON" "$SCRIPT_DIR/hotfix.py" "$@"
