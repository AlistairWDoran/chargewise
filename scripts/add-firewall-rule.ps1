<#
.SYNOPSIS
    Allow LAN devices (e.g. Home Assistant on the Pi) to reach the ChargeWise API.

.DESCRIPTION
    Creates an inbound Windows Firewall rule for TCP port 8000, scoped to the
    local subnet only (192.168.1.0/24) and applied to all network profiles —
    Windows commonly classes home networks as Private, and rules created via
    the "allow access" popup default to Public only, which silently blocks
    Home Assistant.

    Idempotent: safe to run repeatedly. Self-elevating: relaunches itself as
    administrator (one UAC prompt) if not already elevated.

.NOTES
    Run:  Right-click > Run with PowerShell   (accept the UAC prompt)
    Undo: Remove-NetFirewallRule -DisplayName "ChargeWise API 8000"
#>

$RuleName   = "ChargeWise API 8000"
$Port       = 8000
$LanSubnet  = "192.168.1.0/24"

# --- Self-elevate -----------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Requesting administrator rights (UAC prompt)..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`""
    exit
}

# --- Create or update the rule ----------------------------------------------
$existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Rule '$RuleName' already exists — ensuring settings are correct..." -ForegroundColor Cyan
    Set-NetFirewallRule -DisplayName $RuleName -Enabled True -Profile Any -Action Allow
    Set-NetFirewallRule -DisplayName $RuleName -RemoteAddress $LanSubnet
} else {
    Write-Host "Creating rule '$RuleName'..." -ForegroundColor Cyan
    New-NetFirewallRule -DisplayName $RuleName `
                        -Direction  Inbound `
                        -Protocol   TCP `
                        -LocalPort  $Port `
                        -RemoteAddress $LanSubnet `
                        -Action     Allow `
                        -Profile    Any | Out-Null
}

$rule = Get-NetFirewallRule -DisplayName $RuleName
Write-Host ""
Write-Host "Done. '$RuleName': Enabled=$($rule.Enabled), Profile=$($rule.Profile)" -ForegroundColor Green
Write-Host "Home Assistant can now reach the API on port $Port from $LanSubnet."
Write-Host ""
Read-Host "Press Enter to close"
