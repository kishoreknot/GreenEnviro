param(
    [string]$ServiceName = "H2BackgroundService",
    [string]$ExePath = "C:\Program Files\H2Detector\h2_background_service.exe",
    [string]$Arguments = "--mode real --poll-interval 2.0 --db-write-interval 60 --rescan-interval 120",
    [string]$LogDir = "C:\ProgramData\H2GasDetector\logs",
    [string]$NssmPath = "nssm"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $ExePath)) {
    throw "Service executable not found: $ExePath"
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null

$stdoutLog = Join-Path $LogDir "service_stdout.log"
$stderrLog = Join-Path $LogDir "service_stderr.log"

# Remove existing service if present (clean reinstall)
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    & $NssmPath stop $ServiceName | Out-Null
    & $NssmPath remove $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 1
}

# Install service
& $NssmPath install $ServiceName $ExePath $Arguments | Out-Null

# Service metadata
& $NssmPath set $ServiceName DisplayName "H2 Background Polling Service" | Out-Null
& $NssmPath set $ServiceName Description "Polls gas detector devices and writes live/archive data to SQLite." | Out-Null
& $NssmPath set $ServiceName Start SERVICE_AUTO_START | Out-Null

# I/O logs
& $NssmPath set $ServiceName AppStdout $stdoutLog | Out-Null
& $NssmPath set $ServiceName AppStderr $stderrLog | Out-Null
& $NssmPath set $ServiceName AppRotateFiles 1 | Out-Null
& $NssmPath set $ServiceName AppRotateOnline 1 | Out-Null
& $NssmPath set $ServiceName AppRotateBytes 10485760 | Out-Null

# Restart behavior on crash
& $NssmPath set $ServiceName AppExit Default Restart | Out-Null

# Start service
& $NssmPath start $ServiceName | Out-Null
Start-Sleep -Seconds 2

Write-Host "Service installed and started." -ForegroundColor Green
Write-Host ""
Write-Host "Service controls:" -ForegroundColor Yellow
Write-Host "  nssm start $ServiceName"
Write-Host "  nssm stop $ServiceName"
Write-Host "  nssm restart $ServiceName"
Write-Host "  nssm edit $ServiceName"
Write-Host "  nssm remove $ServiceName confirm"
Write-Host ""
Write-Host "Status checks:" -ForegroundColor Yellow
Write-Host "  Get-Service $ServiceName"
Write-Host "  sc query $ServiceName"
Write-Host ""
Write-Host "Logs:" -ForegroundColor Yellow
Write-Host "  Stdout: $stdoutLog"
Write-Host "  Stderr: $stderrLog"
Write-Host ""
Write-Host "Windows UI:" -ForegroundColor Yellow
Write-Host "  services.msc (manage service start/stop/restart/startup type)"
Write-Host "  eventvwr.msc (Application logs for service failures)"
