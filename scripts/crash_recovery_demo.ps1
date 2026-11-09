<#
.SYNOPSIS
  Intentional controller kill/restart demo against a fresh public_01 simulator.

.DESCRIPTION
  Keeps the simulator running. Kills only the controller, runs recovery CLI
  (no time advance), then resumes the same controller DB until the scenario
  finishes or a timeout fires.

  THIS SCRIPT IS DESTRUCTIVE TO THE CHOSEN RUN-DIR / DB PATHS.
  It does not modify scenarios/ or sim/.

  Do not claim success in reports/crash_recovery.md until this script has been
  run and its labelled output reviewed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\crash_recovery_demo.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\scripts\crash_recovery_demo.ps1 -SkipCleanup
#>
[CmdletBinding()]
param(
    [string]$SimUrl = "http://127.0.0.1:8700",
    [string]$Scenario = "scenarios/public_01_dev.yaml",
    [string]$RunDir = "runs/mine01_crash_demo",
    [string]$DbPath = "data/ctrl_public01_crash_demo.sqlite",
    [int]$AdvanceMinutes = 240,
    [int]$ReadyTimeoutSec = 900,
    [int]$PreKillWaitSec = 180,
    [int]$ResumeTimeoutSec = 1800,
    [int]$HealthPollSec = 5,
    [switch]$SkipCleanup
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    throw "Missing venv python at $Py. Create .venv and install requirements first."
}

$env:PYTHONUTF8 = "1"
$env:PYTHONUNBUFFERED = "1"

$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$SimOut = Join-Path $LogDir "crash_demo_sim_$Stamp.log"
$SimErr = Join-Path $LogDir "crash_demo_sim_$Stamp.err"
$CtrlOut = Join-Path $LogDir "crash_demo_ctrl_$Stamp.log"
$CtrlErr = Join-Path $LogDir "crash_demo_ctrl_$Stamp.err"
$RecoveryOut = Join-Path $LogDir "crash_demo_recovery_$Stamp.log"
$ResumeOut = Join-Path $LogDir "crash_demo_resume_$Stamp.log"
$ResumeErr = Join-Path $LogDir "crash_demo_resume_$Stamp.err"
$SummaryOut = Join-Path $LogDir "crash_demo_summary_$Stamp.txt"

function Write-Step([string]$Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
    Write-Host $line
    Add-Content -Path $SummaryOut -Value $line
}

function Test-SimReady {
    try {
        $health = Invoke-RestMethod -Uri "$SimUrl/health" -TimeoutSec 3
        if (-not $health.ok) { return $false }
        $state = Invoke-RestMethod -Uri "$SimUrl/sim/state" -TimeoutSec 5
        return ($null -ne $state.scenario)
    } catch {
        return $false
    }
}

function Get-SimState {
    Invoke-RestMethod -Uri "$SimUrl/sim/state" -TimeoutSec 10
}

function Stop-TrackedProcess($Proc, [string]$Label) {
    if ($null -eq $Proc) { return }
    try {
        if (-not $Proc.HasExited) {
            Write-Step "Stopping $Label pid=$($Proc.Id)"
            Stop-Process -Id $Proc.Id -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
        }
    } catch {
        Write-Step "Stop $Label warning: $_"
    }
}

# Fail fast if something is already bound to the sim port.
if (Test-SimReady) {
    throw "Simulator already responding at $SimUrl. Stop it first so this demo owns a fresh public_01 world."
}

Write-Step "=== crash_recovery_demo start ==="
Write-Step "SimUrl=$SimUrl Scenario=$Scenario RunDir=$RunDir DbPath=$DbPath AdvanceMinutes=$AdvanceMinutes"

if (Test-Path $RunDir) {
    Write-Step "Removing existing run dir $RunDir"
    Remove-Item -Recurse -Force $RunDir
}
Get-ChildItem -Path "$DbPath*" -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

$simProc = $null
$ctrlProc = $null
$resumeProc = $null
$failed = $false

try {
    Write-Step "Starting simulator (force fresh run-dir)"
    $simProc = Start-Process -FilePath $Py -ArgumentList @(
        "-m", "sim", "serve",
        "--scenario", $Scenario,
        "--run-dir", $RunDir,
        "--force"
    ) -PassThru -NoNewWindow -RedirectStandardOutput $SimOut -RedirectStandardError $SimErr

    $deadline = (Get-Date).AddSeconds($ReadyTimeoutSec)
    while (-not (Test-SimReady)) {
        if ((Get-Date) -gt $deadline) {
            throw "Simulator not ready within ${ReadyTimeoutSec}s. See $SimErr"
        }
        if ($simProc.HasExited) {
            throw "Simulator exited early code=$($simProc.ExitCode). See $SimErr"
        }
        Start-Sleep -Seconds $HealthPollSec
    }
    $state0 = Get-SimState
    Write-Step ("Simulator ready scenario={0} sim_minute={1} end_minute={2}" -f $state0.scenario, $state0.sim_minute, $state0.end_minute)

    Write-Step "Starting controller"
    $ctrlProc = Start-Process -FilePath $Py -ArgumentList @(
        "-m", "controller.main",
        "--url", $SimUrl,
        "--db", $DbPath,
        "--advance-minutes", "$AdvanceMinutes"
    ) -PassThru -NoNewWindow -RedirectStandardOutput $CtrlOut -RedirectStandardError $CtrlErr

    Write-Step "Waiting up to ${PreKillWaitSec}s for controller to advance past minute 0"
    $killDeadline = (Get-Date).AddSeconds($PreKillWaitSec)
    $advanced = $false
    while ((Get-Date) -lt $killDeadline) {
        if ($ctrlProc.HasExited) {
            throw "Controller exited before kill window code=$($ctrlProc.ExitCode). See $CtrlErr"
        }
        $st = Get-SimState
        if ([int]$st.sim_minute -gt 0) {
            $advanced = $true
            Write-Step ("Pre-kill state sim_minute={0} finished={1}" -f $st.sim_minute, $st.finished)
            break
        }
        Start-Sleep -Seconds $HealthPollSec
    }
    if (-not $advanced) {
        Write-Step "WARNING: sim_minute still 0 after wait; killing anyway to exercise restart path"
    }

    $minuteBeforeKill = (Get-SimState).sim_minute
    Write-Step "Killing controller only (Stop-Process) pid=$($ctrlProc.Id); simulator stays up"
    Stop-Process -Id $ctrlProc.Id -Force
    Start-Sleep -Seconds 3
    if (-not $ctrlProc.HasExited) {
        throw "Controller did not exit after Stop-Process"
    }
    Write-Step ("Controller dead. Simulator still at sim_minute={0}" -f (Get-SimState).sim_minute)

    Write-Step "Running recovery CLI (no advance) against same DB"
    $recovery = Start-Process -FilePath $Py -ArgumentList @(
        "-m", "controller.recovery",
        "--url", $SimUrl,
        "--db", $DbPath
    ) -PassThru -NoNewWindow -RedirectStandardOutput $RecoveryOut -RedirectStandardError "$RecoveryOut.err"
    $recovery.WaitForExit()
    Write-Step "Recovery exit_code=$($recovery.ExitCode)"
    if ($recovery.ExitCode -ne 0) {
        throw "Recovery CLI failed. See $RecoveryOut / $RecoveryOut.err"
    }
    Get-Content $RecoveryOut | ForEach-Object { Write-Step "recovery: $_" }

    Write-Step "Resuming controller on SAME db=$DbPath"
    $resumeProc = Start-Process -FilePath $Py -ArgumentList @(
        "-m", "controller.main",
        "--url", $SimUrl,
        "--db", $DbPath,
        "--advance-minutes", "$AdvanceMinutes"
    ) -PassThru -NoNewWindow -RedirectStandardOutput $ResumeOut -RedirectStandardError $ResumeErr

    $resumeDeadline = (Get-Date).AddSeconds($ResumeTimeoutSec)
    while ($true) {
        $st = Get-SimState
        if ($st.finished -eq $true) {
            Write-Step ("Simulator finished=true at sim_minute={0}" -f $st.sim_minute)
            break
        }
        if ($resumeProc.HasExited) {
            throw "Resume controller exited early code=$($resumeProc.ExitCode) before sim finished. See $ResumeErr"
        }
        if ((Get-Date) -gt $resumeDeadline) {
            throw "Resume timed out after ${ResumeTimeoutSec}s (sim_minute=$($st.sim_minute)). See $ResumeOut / $ResumeErr"
        }
        Start-Sleep -Seconds $HealthPollSec
    }

    # Give the controller a moment to observe finished and exit cleanly.
    $exitWait = (Get-Date).AddSeconds(120)
    while (-not $resumeProc.HasExited -and (Get-Date) -lt $exitWait) {
        Start-Sleep -Seconds 5
    }
    Write-Step ("Resume controller has_exited={0} exit_code={1}" -f $resumeProc.HasExited, $(if ($resumeProc.HasExited) { $resumeProc.ExitCode } else { "still_running" }))

    Write-Step "Post-checks"
    $final = Get-SimState
    Write-Step ("final finished={0} sim_minute={1} minute_before_kill={2}" -f $final.finished, $final.sim_minute, $minuteBeforeKill)

    $dupProbe = Select-String -Path $ResumeOut, $CtrlOut -Pattern "duplicate|Duplicate proposal" -ErrorAction SilentlyContinue
    if ($dupProbe) {
        Write-Step "WARNING: duplicate-related log lines found — inspect manually"
        $dupProbe | ForEach-Object { Write-Step $_.Line }
    } else {
        Write-Step "No obvious duplicate-proposal log hits in controller stdout"
    }

    Write-Step "SUCCESS criteria for manual review:"
    Write-Step "  1) Simulator stayed up across controller kill"
    Write-Step "  2) Recovery CLI exit 0 against same DB"
    Write-Step "  3) Simulator reached finished=true after resume"
    Write-Step "  4) No duplicate explore/winner POSTs for same proposal_id (check sim_log + DB)"
    Write-Step "Logs: $SummaryOut"
}
catch {
    $failed = $true
    Write-Step "FAILED: $_"
    throw
}
finally {
    if (-not $SkipCleanup) {
        Stop-TrackedProcess $resumeProc "resume-controller"
        Stop-TrackedProcess $ctrlProc "controller"
        Stop-TrackedProcess $simProc "simulator"
        Write-Step "Cleanup complete (use -SkipCleanup to leave processes running)"
    } else {
        Write-Step "SkipCleanup set — leaving processes as-is"
    }
    Write-Step ("=== crash_recovery_demo end failed={0} ===" -f $failed)
}
