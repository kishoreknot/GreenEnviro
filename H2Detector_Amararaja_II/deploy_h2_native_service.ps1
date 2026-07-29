param(
    [string]$SourceExePath = (Join-Path $PSScriptRoot "dist\H2_BackgroundWindowsService.exe"),
    [string]$InstallDir = "C:\Program Files\H2Detector",
    [string]$ServiceName = "H2BackgroundService",
    [string]$ConfigPath = "C:\ProgramData\H2GasDetector\h2_service_config.json",
    [string]$DeployLogPath = "C:\ProgramData\H2GasDetector\logs\deploy_h2_native_service.log"
)

$ErrorActionPreference = "Stop"

function Test-IsAdministrator {
    $currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentIdentity)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Stop-ServiceIfExists {
    param([string]$Name)

    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $svc) {
        return
    }

    if ($svc.Status -ne 'Stopped') {
        Write-Host "Stopping service '$Name' before deployment..." -ForegroundColor Yellow
        sc.exe stop $Name | Out-Null

        $maxWaitSeconds = 20
        $elapsed = 0
        do {
            Start-Sleep -Seconds 1
            $elapsed += 1
            $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
        } while ($svc -and $svc.Status -ne 'Stopped' -and $elapsed -lt $maxWaitSeconds)

        if ($svc -and $svc.Status -ne 'Stopped') {
            throw "Service '$Name' did not stop within $maxWaitSeconds seconds."
        }
    }
}

function Stop-ProcessUsingPath {
    param([string]$FilePath)

    $normalized = [System.IO.Path]::GetFullPath($FilePath)
    $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try {
            $_.Path -and ([System.IO.Path]::GetFullPath($_.Path) -ieq $normalized)
        }
        catch {
            $false
        }
    }

    foreach ($p in $procs) {
        Write-Host "Stopping process locking executable: $($p.ProcessName) (PID $($p.Id))" -ForegroundColor Yellow
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-DataDirectoryWriteAccess {
    param([string]$ConfigFilePath)

    $dataDir = Split-Path -Parent $ConfigFilePath
    if (-not $dataDir) {
        throw "Unable to resolve data directory from ConfigPath: $ConfigFilePath"
    }

    New-Item -ItemType Directory -Path $dataDir -Force | Out-Null

    Write-Host "Applying data directory ACLs for UI + service write access: $dataDir" -ForegroundColor Yellow

    # Ensure inheritance and explicit ACL entries so both service (SYSTEM)
    # and desktop users can read/write SQLite DB (+ WAL/SHM sidecars).
    icacls "$dataDir" /inheritance:e | Out-Null
    icacls "$dataDir" /grant "Users:(OI)(CI)M" "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" /T /C | Out-Null

    # Clear read-only attributes that can block sqlite writes on copied files.
    attrib -R "$dataDir\*" /S /D 2>$null
}

if (-not (Test-IsAdministrator)) {
    $argList = @(
        "-ExecutionPolicy", "Bypass",
        "-File", ('"' + $PSCommandPath + '"'),
        "-SourceExePath", ('"' + $SourceExePath + '"'),
        "-InstallDir", ('"' + $InstallDir + '"'),
        "-ServiceName", ('"' + $ServiceName + '"'),
        "-ConfigPath", ('"' + $ConfigPath + '"'),
        "-DeployLogPath", ('"' + $DeployLogPath + '"')
    )
    Write-Host "Requesting administrator privileges for native service deployment..." -ForegroundColor Yellow
    $elevated = Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList $argList -Verb RunAs -Wait -PassThru
    if ($elevated.ExitCode -ne 0) {
        throw "Elevated deployment process failed with exit code $($elevated.ExitCode)."
    }
    exit 0
}

if (-not (Test-Path $SourceExePath)) {
    throw "Source service executable not found: $SourceExePath"
}

$installerScript = Join-Path $PSScriptRoot "install_h2_native_service.ps1"
if (-not (Test-Path $installerScript)) {
    throw "Installer script not found: $installerScript"
}

$deployLogDir = Split-Path -Parent $DeployLogPath
New-Item -ItemType Directory -Path $deployLogDir -Force | Out-Null

Start-Transcript -Path $DeployLogPath -Force | Out-Null

try {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null

    $targetExePath = Join-Path $InstallDir "H2_BackgroundWindowsService.exe"

    Stop-ServiceIfExists -Name $ServiceName
    Stop-ProcessUsingPath -FilePath $targetExePath

    Ensure-DataDirectoryWriteAccess -ConfigFilePath $ConfigPath

    $copied = $false
    $copyAttempts = 4
    for ($i = 1; $i -le $copyAttempts; $i++) {
        try {
            Copy-Item -Path $SourceExePath -Destination $targetExePath -Force
            $copied = $true
            break
        }
        catch {
            if ($i -eq $copyAttempts) {
                throw
            }
            Write-Host "Copy attempt $i failed due to file lock; retrying..." -ForegroundColor Yellow
            Start-Sleep -Seconds 2
            Stop-ProcessUsingPath -FilePath $targetExePath
        }
    }

    if (-not $copied) {
        throw "Failed to copy service executable after $copyAttempts attempts."
    }

    Write-Host "Copied native service executable to $targetExePath" -ForegroundColor Green

    & $installerScript -ServiceName $ServiceName -ExePath $targetExePath -ConfigPath $ConfigPath

    Write-Host ""
    Write-Host "Deployment complete." -ForegroundColor Green
    Write-Host "Service name: $ServiceName"
    Write-Host "Executable:   $targetExePath"
    Write-Host "Config file:  $ConfigPath"
    Write-Host "Deploy log:   $DeployLogPath"
}
finally {
    Stop-Transcript | Out-Null
}