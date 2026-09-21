# PowerShell runner script for Windows
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $scriptDir

Write-Host "Syncing environment with uv..."
uv sync

Write-Host "Starting server on http://localhost:8000 ..."
Set-Location "$scriptDir\backend"
uv run --project .. uvicorn main:app --host 0.0.0.0 --port 8000 --reload
