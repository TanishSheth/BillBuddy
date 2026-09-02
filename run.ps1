$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".\venv\Scripts\python.exe")) {
    Write-Error "Virtual environment not found. Run: python -m venv venv && .\venv\Scripts\pip install -r requirements.txt"
}

& ".\venv\Scripts\python.exe" run_dev.py
