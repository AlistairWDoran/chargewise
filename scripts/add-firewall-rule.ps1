<#
.SYNOPSIS
    Allow LAN devices (e.g. Home Assistant) to reach the ChargeWise API.

.DESCRIPTION
    Creates an inbound Windows Firewall rule for TCP port 8000, scoped to the
    local subnet only (given as -LanSubnet) and applied to all network profiles —
    Windows commonly classes home networks as Private, and rules created via
    the "allow access" popup default to Public only, which silently blocks
    Home Assistant.

    Idempotent: safe to run repeatedly. Self-elevating: relaunches itself as
    administrator (one UAC prompt) if not already elevated.

.PARAMETER LanSubnet
    Your LAN subnet in CIDR form. Required: there is no default. The form is
    as in 192.0.2.0/24 (a documentation range; use your own).

.EXAMPLE
    .\add-firewall-rule.ps1 -LanSubnet 192.0.2.0/24

.NOTES
    Run:  .\add-firewall-rule.ps1 -LanSubnet <your-subnet>   (accept the UAC prompt)
          or Right-click > Run with PowerShell and type the subnet when asked
    Undo: Remove-NetFirewallRule -DisplayName "ChargeWise API 8000"
#>

param(
    [Parameter(Mandatory = $true, HelpMessage = "Your LAN subnet in CIDR form, e.g. 192.0.2.0/24")]
    [string]$LanSubnet
)

$RuleName   = "ChargeWise API 8000"
$Port       = 8000

# --- Self-elevate -----------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Requesting administrator rights (UAC prompt)..." -ForegroundColor Yellow
    Start-Process powershell -Verb RunAs -ArgumentList "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-LanSubnet", $LanSubnet
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
