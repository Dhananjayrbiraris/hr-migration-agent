#!/usr/bin/env bash
# Sets up and runs the app entirely through uv (https://docs.astral.sh/uv/).
set -e
cd "$(dirname "$0")"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found -- installing (pip fallback; see https://docs.astral.sh/uv/getting-started/installation/ for other options)..."
  pip install --break-system-packages -q uv || curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# Creates/updates .venv from pyproject.toml + uv.lock (fast, reproducible).
uv sync

if [ -f .env ]; then
  echo "Loaded .env -- AI-assisted suggestions will be on if OPENAI_API_KEY is set there."
else
  echo "No .env found (copy .env.example -> .env and add OPENAI_API_KEY to enable AI-assisted suggestions)."
fi

echo "Starting server on http://localhost:8000 ..."
cd backend
uv run --project .. uvicorn main:app --host 0.0.0.0 --port 8000 --reload
