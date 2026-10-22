# apex_desk/scripts/scan_now.ps1
# One-off scan of the current cache (about 2-4 minutes). Use -Asof 2026-04-15 to replay a past day (demo/testing).
param([string]$Asof = "")
Set-Location (Split-Path $PSScriptRoot -Parent)
if ($Asof) { python -m desk scan --asof $Asof } else { python -m desk scan }
