<#
.SYNOPSIS
  Assistant gateway (E2): the MCP server personal AI assistants connect to, at http://localhost:8096/mcp.

.DESCRIPTION
  serve   (default) start the gateway. ask_support needs the agent server on port 8080 (scripts\demo.ps1 or
          ns run); the status tools work without it. Pages: http://localhost:8096/consent (OAuth consent, opened by
          the assistant) and http://localhost:8096/grants (connected assistants, Disconnect).
  token   a test token for one customer (demo), e.g. for Claude Code's --header option.
  eval    the assistant-channel evaluation (scripted model and mock data unless -Live).
  Guide: docs\assistant_gateway.md.

.EXAMPLE
  .\scripts\assistant_gateway.ps1
.EXAMPLE
  .\scripts\assistant_gateway.ps1 token -Customer CUST-S01
.EXAMPLE
  .\scripts\assistant_gateway.ps1 eval -Label e2-scripted
#>
param(
    [Parameter(Position = 0)][ValidateSet('serve', 'token', 'eval')][string]$Command = 'serve',
    [int]$Port = 8096,
    [string]$Customer = '',
    [double]$Days = 1,
    [string]$Label = 'e2-scripted',
    [string]$Ids = '',
    [switch]$Live
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

switch ($Command) {
    'serve' {
        if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
            throw "Port $Port is already in use."
        }
        if (-not (Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue)) {
            Write-Warning 'No agent server on port 8080: ask_support will fail until one runs (scripts\demo.ps1 or ns run).'
        }
        Write-Host "Assistant gateway: http://localhost:$Port/mcp   consent: /consent   connected assistants: http://localhost:$Port/grants"
        $a = @('-m', 'assistant_gateway', 'serve', '--port', "$Port")
    }
    'token' {
        if (-not $Customer) { throw 'token needs -Customer, e.g. -Customer CUST-S01' }
        $a = @('-m', 'assistant_gateway', 'token', '--customer', $Customer, '--days', "$Days")
    }
    'eval' {
        if (Get-NetTCPConnection -State Listen -LocalPort 8080 -ErrorAction SilentlyContinue) {
            throw 'Port 8080 is in use: the evaluation starts its own agent server. Stop the running one first.'
        }
        $a = @('-m', 'evaluation.assistant_eval', '--label', $Label)
        if ($Ids) { $a += '--ids', $Ids }
        if ($Live) { $a += '--live' }
    }
}
$ErrorActionPreference = 'Continue'  # warnings on stderr must not stop Windows PowerShell
& $python @a
exit $LASTEXITCODE
