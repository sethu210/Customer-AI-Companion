<#
.SYNOPSIS
  Personal Stylist Mode (Everly companion plan P5): staged launches, launch, scores, invites, feedback.

.DESCRIPTION
  Works on the newest demo's stores (runtime\demo\stores\console-*) unless -Stores is given, on the simulator's
  clock, like scripts\guardian.ps1:
    staged    products staged for launch and whether they were launched (default)
    launch    launch -Product PRD-...: score every customer with Stylist Mode on and send the earned invites
    scores    the scores of -Product with their breakdown (-Top 10)
    invites   invites (optionally of -Customer)
    run       send held invites that may go now, drop expired ones
    opt-in    switch Stylist Mode on for -Customer (-Undo: off); in the product only the customer does this
    feedback  -Customer -Product -Kind opened | not_for_me | bought
  Guide: docs\companion_plan.md (P5).

.EXAMPLE
  .\scripts\stylist.ps1 opt-in -Customer CUST-B0196
.EXAMPLE
  .\scripts\stylist.ps1 launch -Product PRD-CSC
#>
param(
    [Parameter(Position = 0)][ValidateSet('staged', 'launch', 'scores', 'invites', 'run', 'opt-in', 'feedback')][string]$Command = 'staged',
    [string]$Stores = '',
    [string]$Product = '',
    [string]$Customer = '',
    [string]$Kind = '',
    [int]$Top = 10,
    [switch]$Undo
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root '.venv\Scripts\python.exe'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'

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

switch ($Command) {
    'staged' { $a = @('-m', 'stylist', 'staged') }
    'launch' { if (-not $Product) { throw 'launch needs -Product PRD-...' }; $a = @('-m', 'stylist', 'launch', $Product) }
    'scores' { if (-not $Product) { throw 'scores needs -Product PRD-...' }; $a = @('-m', 'stylist', 'scores', $Product, '--top', "$Top") }
    'invites' { $a = @('-m', 'stylist', 'invites'); if ($Customer) { $a += '--customer', $Customer } }
    'run' { $a = @('-m', 'stylist', 'run') }
    'opt-in' {
        if (-not $Customer) { throw 'opt-in needs -Customer CUST-...' }
        $a = @('-m', 'stylist', 'opt-in', '--customer', $Customer); if ($Undo) { $a += '--undo' }
    }
    'feedback' {
        if (-not $Customer -or -not $Product -or -not $Kind) { throw 'feedback needs -Customer, -Product and -Kind' }
        $a = @('-m', 'stylist', 'feedback', '--customer', $Customer, '--product', $Product, '--kind', $Kind)
    }
}
$ErrorActionPreference = 'Continue'  # warnings on stderr must not stop Windows PowerShell
& $python @a
exit $LASTEXITCODE
