<#
.SYNOPSIS
  Evaluation run through the chat UI: start server + UI with clean stores, drive the conversations through the
  nsflow front end as a user would, score them and write the review document.

.DESCRIPTION
  Like eval.ps1, but starts "ns run" with the nsflow UI (http://localhost:4173) and runs evaluation.ui_runner,
  which uses the same WebSockets as the browser (one fresh session per conversation, identity sent as sly_data).
  Results: runtime\eval\<Label>\<Network>.jsonl (+ _report.md, _metrics.json, _streams\), the stores and server
  log under runtime\eval\<Label>\<Network>\, and the review document docs\test_results\<Label>-<Network>.md.
  Do not edit project files while it runs: the UI's auto-reload would restart the server mid-run.

.EXAMPLE
  .\scripts\ui_eval.ps1 -Network multi -Label ui-full -Trace
.EXAMPLE
  .\scripts\ui_eval.ps1 -Network multi -Label ui-dry -Scripted -Backend mock -Ids S01,S09,M1   # no LLM used
#>
param(
    [Parameter(Mandatory = $true)][ValidateSet('multi', 'lean', 'single')][string]$Network,
    [Parameter(Mandatory = $true)][ValidatePattern('^[A-Za-z0-9_.-]+$')][string]$Label,
    [string[]]$Ids = @(),
    [switch]$Scripted,
    [ValidateSet('', 'mock', 'databricks')][string]$Backend = '',
    [int]$StartupTimeoutSeconds = 180,
    [switch]$Trace
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$ns = Join-Path $root '.venv\Scripts\ns.exe'
$runDir = Join-Path $root "runtime\eval\$Label\$Network"
New-Item -ItemType Directory -Force -Path $runDir | Out-Null

foreach ($port in 8080, 4173) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) {
        throw "Port $port is already in use (a server or UI is running?). Stop it first: this run needs its own settings."
    }
}

$saved = @{}
$settings = @{
    ACTION_MODE      = 'live'
    ACTION_STORE_DIR = (Join-Path $runDir 'actions')
    CASE_STORE_PATH  = (Join-Path $runDir 'cases.db')
    PYTHONIOENCODING = 'utf-8'
    PYTHONUNBUFFERED = '1'
}
if ($Backend) { $settings.CUSTOMER_DATA_BACKEND = $Backend }
if ($Trace) { & (Join-Path $PSScriptRoot 'phoenix.ps1') start }
if (-not (Get-NetTCPConnection -State Listen -LocalPort 6006 -ErrorAction SilentlyContinue)) { $settings.PHOENIX_ENABLED = 'false' }
if ($Scripted) {
    $settings.NS_LLM_CLASS = 'evaluation.scripted_llm.ScriptedSupportLlm'
    $settings.NS_LLM_MODEL = 'scripted'
}
foreach ($k in $settings.Keys) {
    $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
    [Environment]::SetEnvironmentVariable($k, $settings[$k], 'Process')
}

$server = $null
try {
    Write-Host "Starting server + UI (network $Network, label $Label$(if ($Scripted) { ', scripted model' }))..."
    $server = Start-Process -FilePath $ns -ArgumentList 'run' -RedirectStandardOutput (Join-Path $runDir 'server.log') `
        -RedirectStandardError (Join-Path $runDir 'server.err.log') -PassThru -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while (-not ((Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue) -and
                 (Get-NetTCPConnection -State Listen -LocalPort 4173 -ErrorAction SilentlyContinue))) {
        if ($server.HasExited) { throw "Server exited during start-up; see $runDir\server.err.log" }
        if ((Get-Date) -gt $deadline) { throw "Server/UI did not start within $StartupTimeoutSeconds s; see $runDir\server.err.log" }
        Start-Sleep -Seconds 2
    }
    Start-Sleep -Seconds 5  # let the manifest load

    $runnerArgs = @('-m', 'evaluation.ui_runner', '--network', $Network, '--label', $Label)
    if ($Ids.Count) { $runnerArgs += '--ids'; $runnerArgs += $Ids }
    & $python @runnerArgs
    if ($LASTEXITCODE -ne 0) { throw "UI runner failed (exit $LASTEXITCODE)" }
}
finally {
    if ($server -and -not $server.HasExited) {
        & taskkill.exe /T /F /PID $server.Id | Out-Null
        Write-Host 'Server and UI stopped.'
    }
    foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
}

$results = Join-Path $root "runtime\eval\$Label\$Network.jsonl"
& $python -m evaluation.score $results | Out-Null
& $python -m evaluation.review_doc $results
