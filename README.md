# H2Detector

Desktop hydrogen detector monitoring system with:

- A desktop UI executable for operators
- A native Windows background service for polling, alerts, and data persistence

## Solution Overview

### 1. Frontend UI

- Source: `modern_dashboard.py`
- Build output: `dist/H2_Dashboard.exe`
- Purpose:
  - Dashboard and trends
  - Settings, user management, report management
  - Journal visibility

### 2. Background Polling Engine

- Core source: `h2_background_service.py`
- Native Windows service host: `h2_native_windows_service.py`
- Build output (service binary): `dist/H2_BackgroundWindowsService.exe`
- Purpose:
  - Device scanning and polling
  - Live and historical data writes
  - Alert processing
  - Runs as a real Windows service (visible in `services.msc`)

### 3. Data/Domain Layer

- `db_connection.py`: DB and log path resolution
- `db_repository.py`: all DB reads/writes
- `db_schema.py`: schema setup/migrations
- `alert_manager.py`: alert and SMTP behavior
- `auth.py`: user accounts/authentication

## What To Ship To Client (Code-Exposure Safe)

Only share these files:

1. `dist/H2_Dashboard.exe`
2. `dist/H2_BackgroundWindowsService.exe`
3. `deploy_h2_native_service.ps1`
4. `install_h2_native_service.ps1`

Do not ship:

- Any `.py` source files
- Any `.spec` files
- `build/` artifacts
- Development databases from local machine

## Client Installation Steps

### Prerequisites

- Windows machine
- Administrator access for service install

### Install

1. Copy the four deployment files to a folder on client machine, for example:
   - `C:\Install\H2Detector`
2. Open PowerShell as Administrator.
3. Run:

```powershell
PowerShell -ExecutionPolicy Bypass -File C:\Install\H2Detector\deploy_h2_native_service.ps1
```

This deploy script will:

- Elevate if required
- Stop old service/process if needed
- Copy service EXE to `C:\Program Files\H2Detector`
- Install and start the Windows service
- Ensure `C:\ProgramData\H2GasDetector` permissions allow both service and desktop UI writes
- Clear read-only file attributes under `C:\ProgramData\H2GasDetector`
- Write deployment transcript logs

### Verify Service

Run:

```powershell
sc.exe query H2BackgroundService
Get-Service H2BackgroundService
```

You should also see:

- Service name: `H2 Background Polling Service`
- In `services.msc`

### Launch UI

Run:

- `H2_Dashboard.exe`

## Runtime Configuration

Service config file:

- `C:\ProgramData\H2GasDetector\h2_service_config.json`

Important fields:

- `mode`: `real` or `mock`
- `port`: serial COM port (for real mode)
- `baud`: serial baud rate
- `poll_interval`
- `db_write_interval`
- `rescan_interval`
- `scan_device_count`
- `log_level`

Note:

- Config is read with BOM-safe parsing (`utf-8-sig`) for compatibility with PowerShell-generated JSON files.

## Scheduled Report Timing Hint

If report emails arrive at a slightly different time than the configured schedule, this is expected with the current logic.

- Scheduled report runner executes from the background service loop (`h2_background_service.py`), not from the desktop app loop.
- The scheduler checks due events every ~30 seconds.
- If current time is already past the configured schedule and that occurrence was not marked as sent yet, it sends on the next scheduler tick (catch-up behavior).
- Report data window ends at scheduled occurrence minus 1 minute. This affects included data range, not the trigger time itself.

Practical implication:

- Reports continue sending even when no user is logged in to the UI.
- Starting/restarting the background service after a scheduled time can immediately send a pending report on startup ticks.

## Power State Behavior (Sleep / Lock / Hibernate)

### How polling behaves by PC state

- **Lock screen**: Service keeps running and polling continues.
- **Sleep**: CPU/process execution is suspended; polling pauses until wake.
- **Hibernate**: Execution is suspended; polling pauses until resume.
- **Shutdown/Reboot**: Service stops; polling resumes after boot when service auto-starts.

### Backfill and resume

- After wake/resume, the service continues polling.
- Archive backfill logic can fill missed archive interval timestamps using the latest available sample after resume.
- This preserves interval continuity, but cannot recreate true sensor samples while the machine was suspended.

### Can polling continue during Sleep/Hibernate?

- On standard Windows PCs, true Sleep/Hibernate cannot be overridden by app logic to keep polling.
- For near-continuous polling, keep the machine awake during monitoring windows.

### Recommended policy for production monitoring PCs

1. Set power plan to **Never Sleep** on AC power.
2. Disable hibernate if continuous monitoring is required.
3. Keep service startup type as **Automatic**.
4. Use a dedicated always-on PC/server for critical 24x7 monitoring.

## Logs and Data Locations

### Deployment Log

- `C:\ProgramData\H2GasDetector\logs\deploy_h2_native_service.log`

### Runtime Application Log

- `C:\ProgramData\H2GasDetector\h2_dashboard_errors.log`

### Databases

- `C:\ProgramData\H2GasDetector\h2_dashboard.db`
- `C:\ProgramData\H2GasDetector\auth.db`
- `C:\ProgramData\H2GasDetector\alerts.db`

## Service Operations

```powershell
sc.exe start H2BackgroundService
sc.exe stop H2BackgroundService
sc.exe query H2BackgroundService
sc.exe delete H2BackgroundService
```

## Troubleshooting

### Service shows STOPPED with error code 1066

Check:

1. `C:\ProgramData\H2GasDetector\h2_dashboard_errors.log`
2. Windows Event Viewer:
   - Application log
   - System log

### "File in use" during deploy

- Deployment script already handles stop/kill/retry.
- Re-run deployment as Administrator and inspect deploy log.

### "attempt to write a readonly database" / "permission denied" in UI

1. Re-run deployment as Administrator (it now auto-fixes ProgramData ACLs and read-only flags):

```powershell
PowerShell -ExecutionPolicy Bypass -File C:\Install\H2Detector\deploy_h2_native_service.ps1
```

2. Restart service after deployment:

```powershell
sc.exe stop H2BackgroundService
sc.exe start H2BackgroundService
```

3. Launch `H2_Dashboard.exe` again and retry Fetch / Test Report / Settings save.

### No data in UI

1. Confirm service is RUNNING.
2. Confirm `h2_service_config.json` values are correct.
3. Check runtime log for startup or serial errors.

## Repository Housekeeping Checkpoint

Run this checkpoint before each release branch/tag.

### 1) Keep these source/deployment files

- Core app/service code (`modern_dashboard.py`, `h2_background_service.py`, `h2_native_windows_service.py`, `db_*.py`, `auth.py`, `alert_manager.py`, `login_window.py`)
- Active specs (`modern_dashboard.spec`, `h2_native_windows_service.spec`)
- Active deployment scripts (`deploy_h2_native_service.ps1`, `install_h2_native_service.ps1`)
- Documentation (`README.md`, `requirements.txt`)

### 2) Remove generated/local artifacts

- `build/`
- `__pycache__/`
- Local DB and logs in repo root (`*.db`, `*.log`, `*.db-wal`, `*.db-shm`)
- Temporary notes/transcripts (for example `important_chat_session_1.txt`)

### 3) Legacy/optional files to review periodically

- `install_h2_service_nssm.ps1` (legacy NSSM path)
- `build_service_exe.ps1` (machine-specific/legacy build helper)
- `build.spec` and `h2_background_service.spec` (only keep if still used)

### 4) Verify ignore rules are active

- `.gitignore` now ignores cache/build/runtime noise to avoid accidental commits.
- If any generated files are already tracked, run `git rm --cached <file>` once to untrack them.
