<#
.SYNOPSIS
  Start, stop or check the local Phoenix tracing server (http://127.0.0.1:6006).

.DESCRIPTION
  Phoenix runs as its own background process, separate from "ns run": traces survive server restarts, and its
  output goes to a log file (the plugin's built-in autostart pipes the output to a process that never reads
  it). Traces are stored under runtime\phoenix\ (delete that folder to clear them).
  The Neuro SAN plugin (PHOENIX_ENABLED=true in .env) sends traces here whenever Phoenix is running.

.EXAMPLE
  .\scripts\phoenix.ps1 start
.EXAMPLE
  .\scripts\phoenix.ps1 status
.EXAMPLE
  .\scripts\phoenix.ps1 stop
#>
param([Parameter(Mandatory = $true)][ValidateSet('start', 'stop', 'status')][string]$Action)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $root 'runtime\phoenix'
$pidFile = Join-Path $dir 'phoenix.pid'
$port = 6006

function Test-Phoenix { [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) }

switch ($Action) {
    'status' {
        if (Test-Phoenix) { Write-Host "Phoenix is running: http://127.0.0.1:$port" } else { Write-Host 'Phoenix is not running.' }
    }
    'start' {
        if (Test-Phoenix) { Write-Host "Phoenix is already running: http://127.0.0.1:$port"; return }
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $env:PHOENIX_WORKING_DIR = $dir      # trace database location
        $env:PHOENIX_GRPC_PORT = '0'         # gRPC port binding is unreliable on Windows; traces use HTTP
        $env:PHOENIX_HOST = '127.0.0.1'
        $env:PHOENIX_PORT = "$port"
        $p = Start-Process -FilePath (Join-Path $root '.venv\Scripts\python.exe') `
            -ArgumentList '-m', 'phoenix.server.main', 'serve' -WorkingDirectory $root -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $dir 'phoenix.log') -RedirectStandardError (Join-Path $dir 'phoenix.err.log')
        Set-Content -Path $pidFile -Value $p.Id
        foreach ($i in 1..60) {
            if (Test-Phoenix) { Write-Host "Phoenix started: http://127.0.0.1:$port (log: $dir\phoenix.log)"; return }
            if ($p.HasExited) { throw "Phoenix exited during start-up; see $dir\phoenix.err.log" }
            Start-Sleep -Seconds 2
        }
        throw "Phoenix did not open port $port within 120 s; see $dir\phoenix.err.log"
    }
    'stop' {
        $stopped = $false
        if (Test-Path $pidFile) {
            $id = [int](Get-Content $pidFile)
            if (Get-Process -Id $id -ErrorAction SilentlyContinue) { & taskkill.exe /T /F /PID $id | Out-Null; $stopped = $true }
            Remove-Item $pidFile -Force
        }
        if ($stopped) { Write-Host 'Phoenix stopped.' } else { Write-Host 'Phoenix was not started by this script (nothing stopped).' }
    }
}
