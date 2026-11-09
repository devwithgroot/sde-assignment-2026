# Run controller against an already-serving simulator until finished.
# Usage: .\scripts\run_public_scenario.ps1 -SimUrl http://127.0.0.1:8700 -DbPath data/ctrl01.sqlite
param(
    [Parameter(Mandatory = $true)][string]$SimUrl,
    [Parameter(Mandatory = $true)][string]$DbPath,
    [int]$AdvanceMinutes = 240,
    [switch]$NoGovernance
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) { $Py = "python" }
$env:CONTROLLER_ADVANCE_MINUTES = "$AdvanceMinutes"
$argsList = @("-m", "controller.main", "--url", $SimUrl, "--db", $DbPath, "--advance-minutes", "$AdvanceMinutes")
if ($NoGovernance) { $argsList += "--no-governance" }
& $Py @argsList
exit $LASTEXITCODE
