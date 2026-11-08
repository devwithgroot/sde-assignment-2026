# Phase 8 crash recovery helper (Windows).
# Does NOT advance simulated time.
param(
    [Parameter(Mandatory = $true)][string]$SimUrl,
    [string]$DbPath = "data/controller.sqlite"
)
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
Write-Host "crash_recovery: url=$SimUrl db=$DbPath"
& $Py -m controller.recovery --url $SimUrl --db $DbPath
exit $LASTEXITCODE
