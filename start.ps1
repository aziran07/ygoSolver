# Creates .venv on first run, installs dependencies, then starts the app on this PC only.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw "python -m venv failed (exit $LASTEXITCODE). Python 3.13 required." }
}
.venv\Scripts\python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }
.venv\Scripts\python -m streamlit run app.py --server.address localhost --browser.gatherUsageStats false
