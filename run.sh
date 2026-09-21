#!/usr/bin/env bash
set -e

if ! command -v uv &>/dev/null; then
    echo "uv not found — install from https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

uv sync

echo ""
echo "Starting HR Migration Agent (Streamlit)..."
echo "Open http://localhost:8501 in your browser."
echo ""
uv run streamlit run app.py
