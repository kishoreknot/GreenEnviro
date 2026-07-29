# Build script for H2 Background Service executable
# Creates a standalone .exe with all Python code embedded (source code hidden)

# Activate virtual environment
& "C:\Users\Dwansys\GreenEnvCodeBase\Assignment2\.venv\Scripts\Activate.ps1"

# Navigate to h2_dashboard directory
Push-Location "C:\Users\Dwansys\GreenEnvCodeBase\Assignment2\h2_dashboard"

Write-Host "Building h2_background_service.exe..." -ForegroundColor Cyan

# Run PyInstaller with the spec file
pyinstaller h2_background_service.spec --distpath dist --buildpath build --workpath build

if ($LASTEXITCODE -eq 0) {
    Write-Host "✓ Build successful!" -ForegroundColor Green
    Write-Host "Output: dist\h2_background_service.exe" -ForegroundColor Green
    Write-Host ""
    Write-Host "To deploy as Windows Service:" -ForegroundColor Yellow
    Write-Host "  1. Copy dist\h2_background_service.exe to target machine (e.g., C:\Program Files\H2Detector\)"
    Write-Host "  2. Run: nssm install H2BackgroundService `"C:\Program Files\H2Detector\h2_background_service.exe`" `"--mode real`""
    Write-Host "  3. Configure config file at C:\ProgramData\H2GasDetector\h2_service_config.json"
} else {
    Write-Host "✗ Build failed!" -ForegroundColor Red
}

Pop-Location
