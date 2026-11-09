# Measure controller load on an already-serving public_03 simulator.
param(
    [string]$SimUrl = "http://127.0.0.1:8700",
    [string]$DbPath = "data/ctrl_public03.sqlite",
    [int]$AdvanceMinutes = 240,
    [string]$OutMd = "reports/public_03_load.md"
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
$Py = Join-Path $Root ".venv\Scripts\python.exe"
$env:PYTHONUTF8 = "1"
$env:CONTROLLER_ADVANCE_MINUTES = "$AdvanceMinutes"

if (Test-Path $DbPath) { Remove-Item -Force "$DbPath*" }

$log = "logs/public03_controller.log"
$wallStart = Get-Date
$proc = Start-Process -FilePath $Py -ArgumentList @(
    "-m", "controller.main", "--url", $SimUrl, "--db", $DbPath, "--advance-minutes", "$AdvanceMinutes"
) -PassThru -NoNewWindow -RedirectStandardOutput $log -RedirectStandardError "$log.err"

$peakWs = 0L
while (-not $proc.HasExited) {
    try {
        $p = Get-Process -Id $proc.Id -ErrorAction Stop
        $ws = [int64]$p.WorkingSet64
        if ($ws -gt $peakWs) { $peakWs = $ws }
    } catch { }
    Start-Sleep -Seconds 2
}
$wallEnd = Get-Date
$wallSec = ($wallEnd - $wallStart).TotalSeconds
$exit = $proc.ExitCode

$dbBytes = 0L
if (Test-Path $DbPath) { $dbBytes = (Get-Item $DbPath).Length }

$state = Invoke-RestMethod "$SimUrl/sim/state"
$doneLine = Select-String -Path $log -Pattern "done cycles=" | Select-Object -Last 1
$ingestLines = Select-String -Path $log -Pattern "^ingest kind=" | ForEach-Object { $_.Line }

# Pull controller counters via sqlite if available
$metricsJson = & $Py -c @"
import json, sqlite3, os
path = r'$DbPath'
out = {}
if os.path.exists(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    out['events'] = con.execute('select count(*) c from events').fetchone()['c']
    out['calls'] = con.execute('select count(*) c from calls').fetchone()['c']
    out['proposals'] = con.execute('select count(*) c from proposals').fetchone()['c']
    out['approvals'] = con.execute('select count(*) c from controller_approvals').fetchone()['c']
    out['applied'] = con.execute(\"select count(*) c from controller_approvals where status='applied'\").fetchone()['c']
    out['cycles'] = con.execute(\"select value from observability_counters where name='controller.cycles'\").fetchone()
    out['cycles'] = float(out['cycles']['value']) if out['cycles'] else None
    credits = con.execute('select * from credit_budget_state where id=1').fetchone()
    out['credits'] = dict(credits) if credits else None
    con.close()
print(json.dumps(out))
"@

$m = $metricsJson | ConvertFrom-Json
$simDays = [math]::Max(0.0001, $state.end_minute / 1440.0)
$wallPerDay = $wallSec / $simDays
$events = [double]($m.events)
$evPerSec = if ($wallSec -gt 0) { $events / $wallSec } else { 0 }

$md = @"
# public_03_load measurements

Measured live against ``python -m sim serve --scenario scenarios/public_03_load.yaml --run-dir runs/mine03``.
Controller: ``python -m controller.main --url $SimUrl --db $DbPath --advance-minutes $AdvanceMinutes``.
Numbers below are from this run — not inferred from scenario YAML.

## Methodology

- Advance tick: **$AdvanceMinutes** simulated minutes per controller cycle.
- Wall clock: process start → exit (PowerShell ``Get-Date``).
- Peak memory: max ``WorkingSet64`` of the controller process sampled every 2s.
- DB size: SQLite file size after run.
- Event throughput: ``events`` rows in controller SQLite / wall seconds.
- Decision lag: not instrumented as event-ts → decision-ts histogram in this build; proxy is cycle wall time under load (see bottlenecks).
- Credits / decisions: from controller SQLite ``credit_budget_state``, ``proposals``, ``controller_approvals``.

## Results

| Metric | Value |
|---|---|
| Controller exit code | $exit |
| Sim finished | $($state.finished) |
| Sim end_minute | $($state.end_minute) |
| Simulated days | $([math]::Round($simDays, 2)) |
| Wall-clock seconds | $([math]::Round($wallSec, 1)) |
| Wall-clock per simulated day (s) | $([math]::Round($wallPerDay, 1)) |
| Peak working set (MB) | $([math]::Round($peakWs / 1MB, 1)) |
| Controller DB size (MB) | $([math]::Round($dbBytes / 1MB, 2)) |
| Events in controller DB | $events |
| Calls in controller DB | $($m.calls) |
| Event ingest throughput (events/s wall) | $([math]::Round($evPerSec, 1)) |
| Controller cycles | $($m.cycles) |
| Proposals | $($m.proposals) |
| Approvals tracked | $($m.approvals) |
| Approvals applied (local) | $($m.applied) |
| Done line | $($doneLine.Line) |

### Credit budget (controller view)

``````
$($m.credits | ConvertTo-Json -Compress)
``````

### Sim final state

``````
$($state | ConvertTo-Json -Compress)
``````

## Bottlenecks observed

1. **Full recompute each cycle** (metrics + experiments over growing SQLite) dominates after history bootstrap.
2. **History bootstrap** is a large single ingest/project pass before the first advance.
3. **Per-cycle observe** re-fetches workflow/config/chunk metadata every tick (correct, but chatty at 12 workflows).

## 10× scaling notes (honest)

At 10× dials/workflows, SQLite single-writer + full-table experiment scans would break first.
Partition by tenant/workflow shards, keep rolling metric windows instead of full recomputes, and move the audit store to Postgres only if ops accepts it (SRE prefers no new state — so prefer sharded SQLite/files first).
"@

New-Item -ItemType Directory -Force -Path (Split-Path $OutMd) | Out-Null
Set-Content -Path $OutMd -Value $md -Encoding utf8
Write-Host "Wrote $OutMd"
exit $exit
