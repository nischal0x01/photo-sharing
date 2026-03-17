#!/bin/zsh
set -euo pipefail

cd "$(dirname "$0")"

echo "Starting Secure Client Gallery…"

if [[ ! -d ".venv" ]]; then
  echo "Creating virtual environment (.venv)…"
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "Ensuring dependencies are installed…"
python -m pip install --upgrade pip > /dev/null
python -m pip install -r requirements.txt

echo "Launching app…"
python main.py
