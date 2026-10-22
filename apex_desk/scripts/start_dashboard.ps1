# apex_desk/scripts/start_dashboard.ps1
# Starts the dashboard at http://127.0.0.1:8000  (leave this window open)
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m desk serve --port 8000
