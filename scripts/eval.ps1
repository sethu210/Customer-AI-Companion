<#
.SYNOPSIS
  One-command evaluation run: start a clean server, run conversations, score them, stop the server.

.DESCRIPTION
  Starts "ns run --server-only" with clean stores for this label and network, live mode (allowed actions execute
  against the mock system of record) and any requested faults; runs evaluation.runner; scores the results (and
  compares with the other networks of the same label when they exist); always stops the server afterwards.
  Results: runtime\eval\<Label>\<Network>.jsonl, <Network>_report.md, <Network>_metrics.json, and the stores and
  server log under runtime\eval\<Label>\<Network>\.

  A label can be run in chunks (e.g. a few conversations per day): use different -Ids each time. The runner
  refuses to re-run customers whose actions are already in the label's store; use a new label for a clean re-run.

.EXAMPLE
  .\scripts\eval.ps1 -Network multi -Label day1 -Ids S01,S09,M1
.EXAMPLE
  .\scripts\eval.ps1 -Network multi -Label smoke -Scripted -Backend mock      # no LLM provider, no quota used
.EXAMPLE
  .\scripts\eval.ps1 -Network lean -Label day3 -Faults 'get_stuck_cases:timeout:1'
.EXAMPLE
  .\scripts\eval.ps1 -Network multi -Label day1 -Ids S01 -Trace       # also record Phoenix traces
  Phoenix keeps running afterwards; stop it with .\scripts\phoenix.ps1 stop. Each conversation's full message
  stream is always saved to runtime\eval\<Label>\<Network>_streams\<id>.jsonl.
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('multi', 'lean')][string]$Network,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Label,
    [string[]]$Ids = @(),
    [string]$Faults = '',
    [switch]$Scripted,
    [ValidateSet('', 'mock', 'databricks')][string]$Backend = '',
    [int]$Port = 8080,
    [int]$StartupTimeoutSeconds = 180,
    [switch]$Trace  # start Phoenix if needed and record traces (http://127.0.0.1:6006, project customer_service)
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$ns = Join-Path $root '.venv\Scripts\ns.exe'
$runDir = Join-Path $root "runtime\eval\$Label\$Network"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

$listening = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listening) {
    throw "Port $Port is already in use (a server is running?). Stop it first: this run needs its own settings."
}

# Settings for the server and the runner (child processes inherit them; .env does not override them).
$saved = @{}
$settings = @{
    ACTION_MODE          = 'live'
    ACTION_STORE_DIR     = (Join-Path $runDir 'actions')
    CASE_STORE_PATH      = (Join-Path $runDir 'cases.db')
    SIMULATOR_STATE      = (Join-Path $runDir 'simulator.json')  # the Guardian's and the Stylist's clock (fresh)
    CUSTOMER_DATA_FAULTS = $Faults
    PYTHONIOENCODING     = 'utf-8'
}
if ($Backend) { $settings.CUSTOMER_DATA_BACKEND = $Backend }
if ($Trace) { & (Join-Path $PSScriptRoot 'phoenix.ps1') start }
if (-not (Get-NetTCPConnection -State Listen -LocalPort 6006 -ErrorAction SilentlyContinue)) {
    $settings.PHOENIX_ENABLED = 'false'  # no Phoenix running: do not try to export traces
} else {
    Write-Host 'Phoenix is running: traces go to http://127.0.0.1:6006 (project customer_service).'
}
if ($Scripted) {
    $settings.NS_LLM_CLASS = 'evaluation.scripted_llm.ScriptedSupportLlm'
    $settings.NS_LLM_MODEL = 'scripted'
}
foreach ($k in $settings.Keys) {
    $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
    [Environment]::SetEnvironmentVariable($k, $(if ($settings[$k]) { $settings[$k] } else { $null }), 'Process')
}

$server = $null
try {
    Write-Host "Starting server (network $Network, label $Label$(if ($Scripted) { ', scripted model' }))..."
    $server = Start-Process -FilePath $ns -ArgumentList 'run', '--server-only', '--server-http-port', "$Port" `
        -RedirectStandardOutput (Join-Path $runDir 'server.log') -RedirectStandardError (Join-Path $runDir 'server.err.log') `
        -PassThru -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while (-not (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)) {
        if ($server.HasExited) { throw "Server exited during start-up; see $runDir\server.err.log" }
        if ((Get-Date) -gt $deadline) { throw "Server did not start within $StartupTimeoutSeconds s; see $runDir\server.err.log" }
        Start-Sleep -Seconds 2
    }
    Start-Sleep -Seconds 3  # let the manifest load

    $runnerArgs = @('-m', 'evaluation.runner', '--network', $Network, '--label', $Label, '--url', "http://localhost:$Port")
    if ($Ids.Count) { $runnerArgs += '--ids'; $runnerArgs += $Ids }
    & $python @runnerArgs
    if ($LASTEXITCODE -ne 0) { throw "Runner failed (exit $LASTEXITCODE)" }
}
finally {
    if ($server -and -not $server.HasExited) {
        # the server and its child processes; a child that already exited is reported on stderr, which must not
        # stop the script before the results are scored
        $ErrorActionPreference = 'Continue'
        & taskkill.exe /T /F /PID $server.Id 2>$null | Out-Null
        $ErrorActionPreference = 'Stop'
        Write-Host 'Server stopped.'
    }
    foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
}

$results = Join-Path $root "runtime\eval\$Label\$Network.jsonl"
$scoreArgs = @('-m', 'evaluation.score', $results)
$other = @('multi', 'lean') | Where-Object { $_ -ne $Network } |
    ForEach-Object { Join-Path $root "runtime\eval\$Label\$_.jsonl" } | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($other) { $scoreArgs += '--compare'; $scoreArgs += $other }
& $python @scoreArgs
