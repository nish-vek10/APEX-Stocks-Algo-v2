# apex_desk/scripts/start_scheduler.ps1
# Daily jobs: scan 17:20 ET, execute confirmed batch 09:31 ET, snapshot. Leave open (second window).
Set-Location (Split-Path $PSScriptRoot -Parent)
python -m desk scheduler
