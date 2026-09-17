#!/usr/bin/env bash
# Convenience launcher: installs deps (first run only) and starts the app.
set -e
cd "$(dirname "$0")/backend"
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
source .venv/bin/activate
pip install -q -r requirements.txt
echo "Starting server on http://localhost:8000 ..."
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
