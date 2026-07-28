#!/usr/bin/env python3
"""
Native Windows Service host for the H2 background polling engine.

This module exposes a Service Control Manager (SCM) compatible entrypoint so
the polling engine appears as a proper Windows Service in services.msc.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import servicemanager
import win32event
import win32service
import win32serviceutil

from h2_background_service import (
    BackgroundPollingService,
    ServiceConfig,
    build_arg_parser,
    configure_service_logging,
)


logger = logging.getLogger("h2_native_windows_service")


def _resolve_service_data_dir() -> Path:
    env_dir = os.environ.get("H2_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
    return (program_data / "H2GasDetector").resolve()


def _service_config_path() -> Path:
    data_dir = _resolve_service_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "h2_service_config.json"


def _default_service_settings() -> dict:
    parser = build_arg_parser()
    return vars(parser.parse_args([]))


def _load_runtime_settings() -> tuple[ServiceConfig, str]:
    defaults = _default_service_settings()
    config_path = _service_config_path()

    if config_path.exists():
        # PowerShell 5.1 often writes JSON with UTF-8 BOM; utf-8-sig handles both.
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError(f"Service config must be a JSON object: {config_path}")
        defaults.update(raw)
    else:
        config_path.write_text(json.dumps(defaults, indent=2), encoding="utf-8")

    cfg = ServiceConfig(
        mode=str(defaults.get("mode", "real")),
        port=str(defaults.get("port", "COM1")),
        baud=int(defaults.get("baud", 9600)),
        poll_interval=float(defaults.get("poll_interval", 60.0)),
        db_write_interval=float(defaults.get("db_write_interval", 60.0)),
        rescan_interval=float(defaults.get("rescan_interval", 600.0)),
        scan_device_count=int(defaults.get("scan_device_count", 8)),
    )
    return cfg, str(defaults.get("log_level", "INFO"))


class H2BackgroundWindowsService(win32serviceutil.ServiceFramework):
    _svc_name_ = "H2BackgroundService"
    _svc_display_name_ = "H2 Background Polling Service"
    _svc_description_ = "Polls gas detector devices and writes live/archive data in the background."

    def __init__(self, args):
        super().__init__(args)
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._service: BackgroundPollingService | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        logger.info("Stop requested from Windows Service Control Manager")
        if self._service is not None:
            self._service.stop()
        win32event.SetEvent(self._stop_event)

    def SvcDoRun(self):
        cfg, log_level = _load_runtime_settings()
        configure_service_logging(log_level)

        servicemanager.LogInfoMsg(
            f"{self._svc_name_} starting with mode={cfg.mode} port={cfg.port}"
        )

        self._service = BackgroundPollingService(cfg)
        try:
            self._service.start()
            servicemanager.LogInfoMsg(f"{self._svc_name_} stopped normally")
        except Exception as exc:
            logger.exception("Native Windows service host terminated with error")
            servicemanager.LogErrorMsg(f"{self._svc_name_} failed: {exc}")
            raise
        finally:
            win32event.SetEvent(self._stop_event)


def main() -> None:
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(H2BackgroundWindowsService)
        servicemanager.StartServiceCtrlDispatcher()
        return

    win32serviceutil.HandleCommandLine(H2BackgroundWindowsService)


if __name__ == "__main__":
    main()