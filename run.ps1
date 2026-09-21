# Run script for Windows (PowerShell)
$ErrorActionPreference = "Stop"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv not found. Install from https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
}

uv sync

Write-Host ""
Write-Host "Starting HR Migration Agent (Streamlit)..."
Write-Host "Open http://localhost:8501 in your browser."
Write-Host ""

uv run streamlit run app.py
