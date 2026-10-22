# research/dislocation_study/run_all.ps1
# Runs the full dislocation study. From PowerShell:
#   cd C:\Users\ravil\PycharmProjects\APEX-Stocks_algo-v2\research\dislocation_study
#   .\run_all.ps1
# Optional: .\run_all.ps1 -Python "C:\path\to\python.exe"
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host "=== STEP 1/5  merge candles + rebuild stages/signals ===" -ForegroundColor Cyan
& $Python 01_build_merged_features.py
Write-Host "=== STEP 2/5  simulate trades (exact backtest-engine replica) ===" -ForegroundColor Cyan
& $Python 02_simulate_trades.py
Write-Host "=== STEP 3/5  study + Excel report ===" -ForegroundColor Cyan
& $Python 03_dislocation_study.py
Write-Host "=== STEP 4/5  cost stress ===" -ForegroundColor Cyan
& $Python 04_cost_stress.py
Write-Host "=== STEP 5/5  portfolio sim (100k, 1% risk, 75 cap) ===" -ForegroundColor Cyan
& $Python 05_portfolio_sim.py
Write-Host "Done. Report: $PSScriptRoot\output\Dislocation_Study_Report.xlsx" -ForegroundColor Green
