<#
.SYNOPSIS
  Resolution Guardian (E3): status, the event simulator, the watcher and its evaluation.

.DESCRIPTION
  The demo console (scripts\demo.ps1) runs Orbit's clock itself and has the simulator controls in its Inbox tab.
  This script is for the command line and the evaluation:
    status    the watches, their state and the updates (default)
    advance   move the simulator's clock: -Hours 6 or -Days 2, then check the watches
    event     make something happen to a watch: -Watch W-... -Event refund_completed | refund_completed_unconfirmed |
              refund_failed | return_received | parcel_scanned | parcel_delivered | review_approved |
              review_rejected | handoff_resolved
    run       Orbit's clock when no demo console is running: a tick to the agent server every -Every seconds (60)
              until Ctrl+C; Orbit runs the Guardian and Aura. Needs ORBIT_TICK_TOKEN in .env (the agent server's)
    reset     clear the simulator (clock back to the start, no events)
    eval      the time-travel evaluation (scripted model and mock data unless -Live)
  Without -Stores it uses the newest demo's stores (runtime\demo\stores\console-*). Guide: docs\resolution_guardian.md.

.EXAMPLE
  .\scripts\guardian.ps1 status
.EXAMPLE
  .\scripts\guardian.ps1 advance -Days 2
.EXAMPLE
  .\scripts\guardian.ps1 eval -Label e3-scripted
#>
param(
    [Parameter(Position = 0)][ValidateSet('status', 'advance', 'event', 'run', 'reset', 'eval')][string]$Command = 'status',
    [string]$Stores = '',
    [double]$Hours = 0,
    [double]$Days = 0,
    [string]$Watch = '',
    [string]$Event = '',
    [double]$Every = 60,
    [string]$Customer = '',
    [string]$Label = 'e3-scripted',
    [string]$Ids = '',
    [switch]$Live
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

if ($Command -eq 'eval') {
    if (Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue) {
        throw 'Port 8080 is in use: the evaluation starts its own agent server. Stop the running one first.'
    }
    $a = @('-m', 'evaluation.guardian_eval', '--label', $Label)
    if ($Ids) { $a += '--ids', $Ids }
    if ($Live) { $a += '--live' }
}
else {
    if (-not $Stores) {
        $latest = Get-ChildItem (Join-Path $root 'runtime\demo\stores') -Directory -Filter 'console*' -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if (-not $latest) { throw 'No demo stores found; start scripts\demo.ps1 first or pass -Stores <folder>.' }
        $Stores = $latest.FullName
    }
    Write-Host "Stores: $Stores"
    $env:CASE_STORE_PATH = Join-Path $Stores 'cases.db'
    $env:ACTION_STORE_DIR = Join-Path $Stores 'actions'
    $env:SIMULATOR_STATE = Join-Path $Stores 'simulator.json'
    $env:EVENT_SIMULATOR = '1'
    $env:ACTION_MODE = 'live'
    switch ($Command) {
        'status' { $a = @('-m', 'guardian', 'status'); if ($Customer) { $a += '--customer', $Customer } }
        'advance' { $a = @('-m', 'guardian', 'sim', 'advance', '--hours', "$Hours", '--days', "$Days") }
        'event' {
            if (-not $Watch -or -not $Event) { throw 'event needs -Watch W-... and -Event <name> (see status: can_happen)' }
            $a = @('-m', 'guardian', 'sim', 'event', $Watch, $Event)
        }
        'run' { $a = @('-m', 'guardian', 'run', '--every', "$Every") }
        'reset' { $a = @('-m', 'guardian', 'sim', 'reset') }
    }
}
$ErrorActionPreference = 'Continue'  # warnings on stderr must not stop Windows PowerShell
& $python @a
exit $LASTEXITCODE
