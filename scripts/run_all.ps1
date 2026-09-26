#Requires -Version 5.1
<#
.SYNOPSIS
  Windows twin of scripts/run_all.sh: starts the backend, the vision worker and the ngrok HTTPS tunnel,
  and stops all three cleanly on Ctrl+C.

.DESCRIPTION
  Starts, from the repo root, using the project venv (.venv\Scripts\python.exe):

    1. uvicorn  backend.main:app on 0.0.0.0:8000 with --proxy-headers (secure cookies + WebAuthn
       need FastAPI to see https behind the tunnel)
    2. ngrok    http on the PUBLIC_ORIGIN domain from .env, only when config.json
       features.https_tunnel is true and ngrok is on PATH
    3. python -m vision.worker

  Each child writes to its own log under .run\, and this script prints new lines with an
  [api] / [tunnel] / [vision] prefix. Ctrl+C (or closing the window) stops every child.

.PARAMETER Port
  Backend port. Default 8000.

.PARAMETER NoTunnel
  Skip ngrok even when features.https_tunnel is true.

.PARAMETER WorkerArgs
  Anything else is passed to the vision worker, e.g.  .\scripts\run_all.ps1 --no-window

.EXAMPLE
  .\scripts\run_all.ps1
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\run_all.ps1 -NoTunnel
#>

[CmdletBinding()]
param(
    [int] $Port = 8000,
    [switch] $NoTunnel,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $WorkerArgs = @()
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$Py = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path $Py)) {
    Write-Error "No venv at .venv. Create it:  py -3.11 -m venv .venv;  .venv\Scripts\pip.exe install -r requirements.txt"
    exit 1
}

$LogDir = Join-Path $Root '.run'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }

$env:PYTHONUNBUFFERED = '1'

# Each entry: Name, Process, Log path, and how far we have printed.
$script:Children = @()

function Start-Child {
    param(
        [string] $Name,
        [string] $FilePath,
        [string[]] $Arguments
    )
    $out = Join-Path $LogDir "$Name.out.log"
    $err = Join-Path $LogDir "$Name.err.log"
    foreach ($f in @($out, $err)) { Set-Content -Path $f -Value '' -Encoding utf8 }

    $p = Start-Process -FilePath $FilePath -ArgumentList $Arguments -WorkingDirectory $Root `
        -NoNewWindow -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    $script:Children += [pscustomobject]@{ Name = $Name; Process = $p; Out = $out; Err = $err; OutAt = 0; ErrAt = 0 }
    Write-Host "[run_all] $Name pid $($p.Id)"
    return $p
}

function Write-NewLines {
    # Prints whatever each child has written since the last call, with a [name] prefix.
    foreach ($c in $script:Children) {
        foreach ($stream in @('Out', 'Err')) {
            $path = $c.$stream
            $seen = if ($stream -eq 'Out') { $c.OutAt } else { $c.ErrAt }
            if (-not (Test-Path $path)) { continue }
            try { $lines = @(Get-Content -Path $path -ErrorAction Stop) } catch { continue }
            if ($lines.Count -le $seen) { continue }
            foreach ($line in $lines[$seen..($lines.Count - 1)]) {
                if ($null -ne $line -and $line.Trim().Length -gt 0) { Write-Host "[$($c.Name)] $line" }
            }
            if ($stream -eq 'Out') { $c.OutAt = $lines.Count } else { $c.ErrAt = $lines.Count }
        }
    }
}

function Stop-Children {
    # Newest first, so the worker and the tunnel go before the backend they talk to.
    Write-Host '[run_all] stopping...'
    $ordered = @($script:Children)
    [array]::Reverse($ordered)
    foreach ($c in $ordered) {
        $p = $c.Process
        if ($null -eq $p) { continue }
        try { if ($p.HasExited) { continue } } catch { continue }
        # /T kills the whole tree (uvicorn reloaders, ngrok helpers); /F because console apps
        # have no window to close.
        & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null
    }
    foreach ($c in $ordered) {
        $p = $c.Process
        if ($null -eq $p) { continue }
        try { $null = $p.WaitForExit(5000) } catch { }
    }
    Write-NewLines
    Write-Host '[run_all] stopped'
}

# Backstop for a closed window / `exit`; the finally block covers Ctrl+C.
$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action { Stop-Children }

try {
    # --- 1. backend -------------------------------------------------------------------------------
    $api = Start-Child -Name 'api' -FilePath $Py -Arguments @(
        '-m', 'uvicorn', 'backend.main:app',
        '--host', '0.0.0.0', '--port', "$Port", '--no-access-log',
        '--proxy-headers'
    )

    # Wait for /api/health so the first snapshots do not all fail.
    $ready = $false
    foreach ($i in 1..40) {
        if ($api.HasExited) { break }
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/health" -UseBasicParsing -TimeoutSec 1
            if ($r.StatusCode -eq 200) { $ready = $true; break }
        } catch { }
        Start-Sleep -Milliseconds 500
        Write-NewLines
    }
    if (-not $ready) {
        Write-NewLines
        Write-Host '[run_all] backend did not answer /api/health' -ForegroundColor Red
        exit 1
    }
    Write-Host "[run_all] backend ready on http://localhost:$Port"

    # --- 2. HTTPS tunnel (F14, S3.1) --------------------------------------------------------------
    # The ngrok static domain comes from PUBLIC_ORIGIN; it must never change or every passkey breaks.
    $tunnelOn = $false
    try {
        $cfg = Get-Content -Raw -Path (Join-Path $Root 'config.json') | ConvertFrom-Json
        $tunnelOn = [bool] $cfg.features.https_tunnel
    } catch {
        Write-Host '[run_all] could not read config.json features.https_tunnel; tunnel not started'
    }

    if ($tunnelOn -and -not $NoTunnel) {
        $origin = ''
        $envPath = Join-Path $Root '.env'
        if (Test-Path $envPath) {
            foreach ($line in Get-Content $envPath) {
                if ($line -match '^\s*PUBLIC_ORIGIN\s*=\s*(.+)$') {
                    $origin = $matches[1].Trim().Trim('"').Trim("'").Split('#')[0].Trim().TrimEnd('/')
                }
            }
        }
        $domain = ''
        if ($origin.StartsWith('https://')) { $domain = $origin.Substring('https://'.Length) }

        $ngrok = $null
        try { $ngrok = (Get-Command ngrok -ErrorAction Stop).Source } catch { }

        if ($domain -eq '') {
            Write-Host '[run_all] https_tunnel is on but PUBLIC_ORIGIN in .env is not an https:// URL; tunnel not started'
        } elseif ($null -eq $ngrok) {
            Write-Host '[run_all] https_tunnel is on but ngrok is not on PATH; tunnel not started'
        } else {
            # ngrok >= 3.16 takes --url; older v3 releases only know --domain.
            $flag = '--domain'
            try { if ((& $ngrok http --help 2>&1 | Out-String) -match '--url') { $flag = '--url' } } catch { }
            Start-Child -Name 'tunnel' -FilePath $ngrok -Arguments @(
                'http', "$flag=$domain", "$Port", '--log', 'stdout', '--log-format', 'logfmt'
            ) | Out-Null
            Write-Host "[run_all] tunnel: https://$domain -> localhost:$Port"
        }
    } elseif ($NoTunnel) {
        Write-Host '[run_all] -NoTunnel given; tunnel not started'
    }

    # --- 3. vision worker -------------------------------------------------------------------------
    $visionArgs = @('-m', 'vision.worker') + $WorkerArgs
    $vision = Start-Child -Name 'vision' -FilePath $Py -Arguments $visionArgs

    Write-Host '[run_all] Ctrl+C stops everything.'

    # --- monitor ----------------------------------------------------------------------------------
    $visionReported = $false
    while (-not $api.HasExited) {
        Write-NewLines
        if (-not $visionReported -and $vision.HasExited) {
            Write-Host "[run_all] vision worker exited; backend still running. Restart it with: $Py -m vision.worker"
            $visionReported = $true
        }
        Start-Sleep -Milliseconds 400
    }
    Write-NewLines
    Write-Host '[run_all] backend exited'
} finally {
    Stop-Children
    Unregister-Event -SourceIdentifier PowerShell.Exiting -ErrorAction SilentlyContinue
}
