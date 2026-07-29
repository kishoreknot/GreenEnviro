param(
    [string]$ServiceName = "H2BackgroundService",
    [string]$ExePath = "C:\Program Files\H2Detector\H2_BackgroundWindowsService.exe",
    [string]$ConfigPath = "C:\ProgramData\H2GasDetector\h2_service_config.json"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ExePath)) {
    throw "Native service executable not found: $ExePath"
}

$configDir = Split-Path -Parent $ConfigPath
New-Item -ItemType Directory -Path $configDir -Force | Out-Null

if (-not (Test-Path $ConfigPath)) {
    @"
{
  "mode": "mock",
  "port": "COM1",
  "baud": 9600,
  "poll_interval": 1.0,
  "db_write_interval": 60.0,
  "rescan_interval": 120.0,
  "log_level": "INFO",
  "scan_device_count": 20
}
"@ | Set-Content -Path $ConfigPath -Encoding UTF8
}

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    sc.exe stop $ServiceName | Out-Null
    Start-Sleep -Seconds 1
    sc.exe delete $ServiceName | Out-Null
    Start-Sleep -Seconds 2
}

sc.exe create $ServiceName binPath= "`"$ExePath`"" start= auto obj= LocalSystem DisplayName= "H2 Background Polling Service" | Out-Null
sc.exe description $ServiceName "Polls gas detector devices and writes live/archive data in the background." | Out-Null
sc.exe failure $ServiceName reset= 86400 actions= restart/60000/restart/60000/restart/60000 | Out-Null
sc.exe failureflag $ServiceName 1 | Out-Null
sc.exe start $ServiceName | Out-Null

Write-Host "Native Windows service installed and started." -ForegroundColor Green
Write-Host ""
Write-Host "Service controls:" -ForegroundColor Yellow
Write-Host "  sc.exe start $ServiceName"
Write-Host "  sc.exe stop $ServiceName"
Write-Host "  sc.exe query $ServiceName"
Write-Host "  sc.exe delete $ServiceName"
Write-Host ""
Write-Host "Configuration:" -ForegroundColor Yellow
Write-Host "  $ConfigPath"
Write-Host ""
Write-Host "Service UI:" -ForegroundColor Yellow
Write-Host "  services.msc"