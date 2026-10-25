<#
.SYNOPSIS
  Everly demo console (the customer's AI companion at ACME Retail): starts the agent server (live mode, fresh stores) and the console at
  http://127.0.0.1:8095. Ctrl+C stops both.

.DESCRIPTION
  Customer data comes from CUSTOMER_DATA_BACKEND in .env (Databricks for demos). Each start uses fresh case and
  action stores under runtime\demo\stores\console-<time>\, so demo scenarios start from the same data (an action
  done in one demo would otherwise be "already handled" in the next). Recording is off unless -Record is given:
  then the console shows a Record toggle and saves each recorded conversation as a take
  (runtime\demo\recordings\).
  Orbit's clock: every 60 s the console sends Orbit a tick, and Orbit calls the Resolution Guardian (follow-up checks)
  and Aura (held picks) through the agent network (docs\orbit_tick_plan.md). The ticks carry a secret,
  ORBIT_TICK_TOKEN, given to both processes: from .env if set there, otherwise a new random one per start. The Inbox
  tab shows the customer's updates and has the event simulator controls (advance the clock, refund completes, parcel
  delivered, ...). Guide: docs\resolution_guardian.md.

  -Assistant also starts the assistant gateway (personal AI assistants over MCP, http://localhost:8096/mcp) with this
  demo's stores, so a signed-in customer's own assistant shows on the map: its box on the left and its ask_support
  turns in the background lane (docs\assistant_lane_plan.md). A test token: .\scripts\assistant_gateway.ps1 token
  -Customer CUST-S01 (grants stay in runtime\assistant\assistant.db across demos).

.EXAMPLE
  .\scripts\demo.ps1
.EXAMPLE
  .\scripts\demo.ps1 -Record
.EXAMPLE
  .\scripts\demo.ps1 -Assistant
#>
param(
    [switch]$Record,
    [switch]$Assistant,
    [int]$Port = 8095,
    [switch]$KeepStores,
    [switch]$Trace,
    [int]$StartupTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$ns = Join-Path $root '.venv\Scripts\ns.exe'
foreach ($p in @(8080, $Port) + $(if ($Assistant) { @(8096) } else { @() })) {
    if (Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue) {
        throw "Port $p is already in use (an agent server, nsflow or another console?). Stop it first."
    }
}
$stores = Join-Path $root ("runtime\demo\stores\" + $(if ($KeepStores) { 'console' } else { 'console-' + (Get-Date -Format 'yyyyMMdd-HHmmss') }))
New-Item -ItemType Directory -Force -Path $stores | Out-Null

$settings = @{
    ACTION_MODE      = 'live'
    ACTION_STORE_DIR = (Join-Path $stores 'actions')
    CASE_STORE_PATH  = (Join-Path $stores 'cases.db')
    PYTHONIOENCODING = 'utf-8'
    PYTHONUNBUFFERED = '1'
    DEMO_RECORDING   = $(if ($Record) { '1' } else { '0' })
    # Resolution Guardian (E3): the local event simulator, its clock and events kept with this demo's stores
    EVENT_SIMULATOR  = '1'
    SIMULATOR_STATE  = (Join-Path $stores 'simulator.json')
    # Orbit's clock ticks: the same secret for the agent server and the console
    ORBIT_TICK_TOKEN = [guid]::NewGuid().ToString('N')
    # Assistant gateway (-Assistant): the demo's "choose a test customer" sign-in next to the emailed code. Allowed
    # only on this computer (loopback); the gateway refuses to start with it anywhere else.
    ASSISTANT_DEMO_SIGNIN = '1'
}
$dotenv = Join-Path $root '.env'
if (Test-Path $dotenv) {
    $line = Select-String -Path $dotenv -Pattern '^\s*ORBIT_TICK_TOKEN\s*=\s*(\S+)' | Select-Object -First 1
    if ($line) { $settings.ORBIT_TICK_TOKEN = $line.Matches[0].Groups[1].Value }
}
if ($Trace) { & (Join-Path $PSScriptRoot 'phoenix.ps1') start }
if (-not (Get-NetTCPConnection -State Listen -LocalPort 6006 -ErrorAction SilentlyContinue)) { $settings.PHOENIX_ENABLED = 'false' }
$saved = @{}
foreach ($k in $settings.Keys) {
    $saved[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
    [Environment]::SetEnvironmentVariable($k, $settings[$k], 'Process')
}

$server = $null
$gateway = $null
try {
    Write-Host "Starting the agent server (stores: $stores) ..."
    $server = Start-Process -FilePath $ns -ArgumentList 'run', '--server-only', '--server-http-port', '8080' `
        -RedirectStandardOutput (Join-Path $stores 'server.log') -RedirectStandardError (Join-Path $stores 'server.err.log') `
        -PassThru -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds($StartupTimeoutSeconds)
    while (-not (Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue)) {
        if ($server.HasExited) { throw "Agent server exited during start-up; see $stores\server.err.log" }
        if ((Get-Date) -gt $deadline) { throw "Agent server did not start within $StartupTimeoutSeconds s" }
        Start-Sleep -Seconds 2
    }
    Start-Sleep -Seconds 5
    if ($Assistant) {  # same stores (inherited); grants in the default assistant store, where test tokens go
        $gateway = Start-Process -FilePath $python -ArgumentList '-m', 'assistant_gateway', 'serve', '--port', '8096' `
            -RedirectStandardOutput (Join-Path $stores 'gateway.log') -RedirectStandardError (Join-Path $stores 'gateway.err.log') `
            -PassThru -WindowStyle Hidden
        Write-Host 'Assistant gateway: http://localhost:8096/mcp   (test token: .\scripts\assistant_gateway.ps1 token -Customer CUST-S01)'
    }
    Write-Host "Demo console: http://127.0.0.1:$Port$(if ($Record) { '   (Record toggle on)' })   Ctrl+C stops it."
    $ErrorActionPreference = 'Continue'  # the console logs to stderr; Windows PowerShell must not treat that as an error
    & $python -m demo_console --port $Port
}
finally {
    if ($gateway -and -not $gateway.HasExited) {
        & taskkill.exe /T /F /PID $gateway.Id | Out-Null
        Write-Host 'Assistant gateway stopped.'
    }
    if ($server -and -not $server.HasExited) {
        & taskkill.exe /T /F /PID $server.Id | Out-Null
        Write-Host 'Agent server stopped.'
    }
    foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k], 'Process') }
}
