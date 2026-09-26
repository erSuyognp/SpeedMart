#Requires -Version 5.1
<#
.SYNOPSIS
    Frees the store between judges: logs in as admin and calls POST /admin/reset (8.3).

.DESCRIPTION
    Cancels the active session, clears the overrides, releases the store lock and sets the LEDs idle.
    Reads ADMIN_PASSWORD from .env in the repo root unless -Password is given. The Windows twin of
    scripts/reset_demo.sh.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\reset_demo.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\reset_demo.ps1 -Url http://127.0.0.1:8000
#>
[CmdletBinding()]
param(
    [string] $Url = 'http://127.0.0.1:8000',
    [string] $Password,
    [string] $EnvFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
if (-not $EnvFile) { $EnvFile = Join-Path $root '.env' }
$Url = $Url.TrimEnd('/')

function Read-DotEnvValue {
    param([string] $Path, [string] $Key)

    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
        $split = $trimmed.IndexOf('=')
        if ($split -lt 1) { continue }
        if ($trimmed.Substring(0, $split).Trim() -ne $Key) { continue }
        $value = $trimmed.Substring($split + 1).Trim()
        # Strip one layer of surrounding quotes, the way python-dotenv does.
        if ($value.Length -ge 2 -and
            (($value.StartsWith('"') -and $value.EndsWith('"')) -or
             ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        return $value
    }
    return $null
}

if (-not $Password) { $Password = Read-DotEnvValue -Path $EnvFile -Key 'ADMIN_PASSWORD' }
if (-not $Password) { $Password = $env:ADMIN_PASSWORD }
if (-not $Password) {
    Write-Error "ADMIN_PASSWORD not found in $EnvFile or the environment. Pass -Password instead."
    exit 2
}

# One session for both calls, so the admin cookie from /admin/login is sent to /admin/reset.
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession

try {
    Invoke-RestMethod -Method Post -Uri "$Url/admin/login" -WebSession $session `
        -ContentType 'application/json' `
        -Body (@{ password = $Password } | ConvertTo-Json -Compress) | Out-Null
}
catch {
    Write-Host "Admin login failed against $Url." -ForegroundColor Red
    Write-Host "  $($_.Exception.Message)"
    Write-Host '  Is the backend running, and does ADMIN_PASSWORD match the one it started with?'
    exit 1
}

try {
    $result = Invoke-RestMethod -Method Post -Uri "$Url/admin/reset" -WebSession $session `
        -ContentType 'application/json' -Body '{}'
}
catch {
    Write-Host "Reset failed against $Url." -ForegroundColor Red
    Write-Host "  $($_.Exception.Message)"
    exit 1
}

$cancelled = $null
if ($result.PSObject.Properties.Name -contains 'cancelled_session_id') {
    $cancelled = $result.cancelled_session_id
}

Write-Host 'Store reset.' -ForegroundColor Green
if ($cancelled) {
    Write-Host "  Cancelled session $cancelled. Overrides cleared, lock free, LEDs idle."
}
else {
    Write-Host '  No session was active. Overrides cleared, lock free, LEDs idle.'
}
Write-Host "  $($result | ConvertTo-Json -Compress)"
exit 0
