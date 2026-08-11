<#
================================================================================
 CHEP PQAS Gocator — Grafana Alloy installer (per Windows PC)

 What it does:
   1. Reads all credentials and site config from config.env (two levels up).
   2. Sets machine-level environment variables.
   3. Copies config.alloy into the Alloy program directory.
   4. (Re)starts the Alloy Windows service.

 Run from an ELEVATED PowerShell (Run as Administrator):
   .\install-alloy.ps1

 Before running: edit config.env at the repo root — set SITE_ID, SITE_NAME,
 STATE, and GCLOUD_* values for this specific PC.
================================================================================
#>

#requires -RunAsAdministrator
$ErrorActionPreference = "Stop"

# --------------------------------------------------------------------------
# Load config.env (repo root, two levels above this script)
# --------------------------------------------------------------------------
$ConfigPath = Join-Path $PSScriptRoot "..\..\config.env"
if (-not (Test-Path $ConfigPath)) {
    Write-Error "config.env not found at $ConfigPath. Create it from the template and fill in all values."
    exit 1
}

$cfg = @{}
foreach ($line in Get-Content $ConfigPath) {
    if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
    $key, $val = $line -split '=', 2
    $cfg[$key.Trim()] = $val.Trim()
}

$SITE_ID            = $cfg["SITE_ID"]
$SITE_NAME          = $cfg["SITE_NAME"]
$STATE              = $cfg["STATE"]
$GCLOUD_LOKI_URL    = $cfg["GCLOUD_LOKI_URL"]
$GCLOUD_LOKI_USER   = $cfg["GCLOUD_LOKI_USER"]
$GCLOUD_PROM_URL    = $cfg["GCLOUD_PROM_URL"]
$GCLOUD_PROM_USER   = $cfg["GCLOUD_PROM_USER"]
$GCLOUD_API_TOKEN   = $cfg["GCLOUD_API_TOKEN"]
$GOCATOR_LOG_PATH   = $cfg["LOG_PATH"]
$GOCATOR_STATE_PATH = $cfg["STATE_PATH"]

$required = @("SITE_ID","SITE_NAME","STATE","GCLOUD_LOKI_URL","GCLOUD_LOKI_USER",
              "GCLOUD_PROM_URL","GCLOUD_PROM_USER","GCLOUD_API_TOKEN","LOG_PATH","STATE_PATH")
foreach ($k in $required) {
    if (-not $cfg[$k]) { Write-Error "config.env is missing: $k"; exit 1 }
}

# --------------------------------------------------------------------------
function Set-MachineEnv($name, $value) {
    [Environment]::SetEnvironmentVariable($name, $value, "Machine")
    Write-Host "  $name = $value"
}

Write-Host "[1/4] Setting machine environment variables..." -ForegroundColor Cyan
Set-MachineEnv "SITE_ID"            $SITE_ID
Set-MachineEnv "SITE_NAME"          $SITE_NAME
Set-MachineEnv "STATE"              $STATE
Set-MachineEnv "GCLOUD_LOKI_URL"    $GCLOUD_LOKI_URL
Set-MachineEnv "GCLOUD_LOKI_USER"   $GCLOUD_LOKI_USER
Set-MachineEnv "GCLOUD_PROM_URL"    $GCLOUD_PROM_URL
Set-MachineEnv "GCLOUD_PROM_USER"   $GCLOUD_PROM_USER
Set-MachineEnv "GCLOUD_API_TOKEN"   $GCLOUD_API_TOKEN
Set-MachineEnv "GOCATOR_LOG_PATH"   $GOCATOR_LOG_PATH
Set-MachineEnv "GOCATOR_STATE_PATH" $GOCATOR_STATE_PATH

Write-Host "[2/4] Checking Alloy install..." -ForegroundColor Cyan
$AlloyDir    = "C:\Program Files\GrafanaLabs\Alloy"
$AlloyConfig = "$AlloyDir\config.alloy"
$SourceConfig = Join-Path $PSScriptRoot "..\alloy\config.alloy"

if (-not (Test-Path "$AlloyDir\alloy-windows-amd64.exe")) {
    Write-Warning "Alloy not found in $AlloyDir."
    Write-Host    "Install it first:"
    Write-Host    "    winget install --id Grafana.Alloy"
    Write-Host    "Then re-run this script."
    exit 1
}

Write-Host "[3/4] Deploying config.alloy..." -ForegroundColor Cyan
Copy-Item -Path $SourceConfig -Destination $AlloyConfig -Force
Write-Host "  Copied to $AlloyConfig"

Write-Host "[4/4] Restarting Alloy service..." -ForegroundColor Cyan
$svc = Get-Service -Name "Alloy" -ErrorAction SilentlyContinue
if ($null -eq $svc) {
    Write-Warning "Service 'Alloy' not found. Install Alloy via MSI/winget so it registers the service."
    exit 1
}
Set-Service -Name "Alloy" -StartupType Automatic
Restart-Service -Name "Alloy"
Start-Sleep -Seconds 3
Get-Service -Name "Alloy" | Format-Table -AutoSize

Write-Host ""
Write-Host "Done. Site: $SITE_NAME ($SITE_ID)" -ForegroundColor Green
Write-Host "Verify locally at http://127.0.0.1:12345 (Alloy UI)." -ForegroundColor Green
Write-Host "Then check Grafana Cloud: {site=`"$SITE_ID`"} in Explore (Loki)." -ForegroundColor Green
