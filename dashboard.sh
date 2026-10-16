#!/usr/bin/env bash
# Inicia o ZCRM Release Dashboard usando o venv do projeto.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"

if [[ ! -f "$VENV/bin/activate" ]]; then
    echo "Criando venv..."
    python3 -m venv "$VENV"
fi

source "$VENV/bin/activate"

if ! "$VENV/bin/python3" -c "import flask" 2>/dev/null; then
    echo "Instalando Flask..."
    "$VENV/bin/pip" install --quiet flask
fi

# Garante que o grupo 'docker' está ativo para o processo do dashboard.
# Necessário quando o usuário foi adicionado ao grupo docker mas ainda não fez
# logout/login completo (sg aplica o grupo sem precisar reiniciar a sessão).
if getent group docker &>/dev/null; then
    exec sg docker -c "\"$VENV/bin/python3\" \"$SCRIPT_DIR/dashboard.py\""
else
    exec "$VENV/bin/python3" "$SCRIPT_DIR/dashboard.py"
fi
