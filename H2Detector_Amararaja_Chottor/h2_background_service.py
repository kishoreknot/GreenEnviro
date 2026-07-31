#!/usr/bin/env python3
"""
Background polling service for H2 Gas Detector Dashboard.

Responsibilities:
- Establish Modbus serial connection
- Scan devices and keep address list current
- Poll readings continuously
- Store per-minute readings in database
- Trigger configured alerts

This process is designed to run independently from the desktop UI.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import random
import struct
import sys
import threading
import time
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Single-instance guard (Windows named mutex)
# ---------------------------------------------------------------------------
_MUTEX_NAME = "Global\\H2BackgroundServiceMutex"
_mutex_handle = None

def _acquire_single_instance_lock() -> bool:
    """Return True if this is the only running instance, False if another exists."""
    global _mutex_handle
    try:
        import ctypes
        _mutex_handle = ctypes.windll.kernel32.CreateMutexW(None, True, _MUTEX_NAME)
        last_err = ctypes.windll.kernel32.GetLastError()
        if last_err == 183:  # ERROR_ALREADY_EXISTS
            return False
        return True
    except Exception:
        return True  # non-Windows or ctypes unavailable — allow startup

from db_schema import initialise_schema
from db_repository import (
    get_default_plant_id,
    upsert_device,
    get_device_name,
    get_all_devices,
    apply_scan_status_flags,
    reset_all_devices_offline,
    get_k_factor_for_device_address,
    insert_reading,
    upsert_live_reading,
)
from alert_manager import (
    init_alert_db,
    check_and_fire,
    check_and_fire_offline_alert,
    run_pending_scheduled_reports,
)

try:
    import serial
    SERIAL_AVAILABLE = True
except Exception:
    SERIAL_AVAILABLE = False


logger = logging.getLogger("h2_background_service")

SCAN_TIMEOUT = 0.15
REPORT_SCHEDULER_TICK_SECONDS = 30.0

# Number of extra poll attempts for a known DB device that does not respond
# during the startup initial scan (total attempts = 1 + STARTUP_SCAN_POLL_RETRIES).
STARTUP_SCAN_POLL_RETRIES = 2

GAS_TYPE_MAP = {
    0x0001: "CO", 0x0002: "H2S", 0x0003: "O2", 0x0004: "LEL",
    0x0005: "CO2", 0x0006: "NH3", 0x0007: "H2", 0x0008: "Cl2",
    0x0009: "NO2", 0x000A: "SO2", 0x000B: "NO", 0x000C: "HF",
    0x001D: "VOC", 0x002A: "CH4", 0x002B: "C3H8", 0x002C: "C4H10",
}
GAS_UNIT_MAP = {0: "ppm", 1: "%LEL", 2: "%VOL", 3: "mg/m3", 4: "%"}


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 0x0001) else crc >> 1
    return crc


def build_scan_command(address: int) -> bytes:
    payload = bytes([address, 0x03, 0xA0, 0x29, 0x00, 0x11])
    crc = crc16_modbus(payload)
    return payload + struct.pack("<H", crc)


def parse_device_response(raw: bytes):
    if len(raw) < 39:
        return None
    if raw[1] != 0x03 or raw[2] != 0x22:
        return None
    crc_recv = struct.unpack_from("<H", raw, 37)[0]
    if crc_recv != crc16_modbus(raw[:37]):
        return None

    d = raw[3:37]
    gas_type = struct.unpack_from(">H", d, 0)[0]
    gas_unit = struct.unpack_from(">H", d, 2)[0]
    decimal_pl = struct.unpack_from(">H", d, 4)[0]
    dev_range = struct.unpack_from(">I", d, 6)[0]
    conc = struct.unpack_from(">I", d, 10)[0]
    low_alarm = struct.unpack_from(">I", d, 14)[0]
    high_alarm = struct.unpack_from(">I", d, 18)[0]
    alarm_st = struct.unpack_from(">H", d, 30)[0]

    div = 10 ** decimal_pl if decimal_pl <= 4 else 1
    return {
        "address": raw[0],
        "gas_name": GAS_TYPE_MAP.get(gas_type, f"Gas-{gas_type:04X}"),
        "gas_unit": "ppm",
        "device_range": round(dev_range / div, decimal_pl),
        "concentration": round(conc / div, decimal_pl),
        "low_alarm": round(low_alarm * 10), # / div, decimal_pl), GEnvCSTM - AMARRAJA
        "high_alarm": round(high_alarm * 10), #  / div, decimal_pl),GEnvCSTM - AMARRAJA -- Multiple by 10 as per customer request.
        "alarm_status": alarm_st,
        "online": True,
    }


class MockScanner:
    # MOCK_ADDRESSES = [8, 9, 11, 12, 15]
    MOCK_ADDRESSES = [8, 9, 11, 12]

    def __init__(self):
        self._base_conc = {a: random.uniform(5, 80) for a in self.MOCK_ADDRESSES}
        self._gas_types = {a: 0x001D for a in self.MOCK_ADDRESSES}
        self._alarm_seeds = {}
        for a in self.MOCK_ADDRESSES:
            lo = round(random.uniform(20, 40), 1)
            self._alarm_seeds[a] = (lo, round(lo * 2, 1))

    def scan(self, on_progress=None):
        found = []
        total = len(self.MOCK_ADDRESSES)
        for idx, addr in enumerate(self.MOCK_ADDRESSES, start=1):
            if on_progress:
                on_progress(idx, total)
            time.sleep(0.002)
            c_init = round(self._base_conc[addr], 1)
            lo, hi = self._alarm_seeds[addr]
            alarm_init = 3 if c_init >= hi else (2 if c_init >= lo else 1)
            raw = self._make_fake_response(addr, concentration=c_init, alarm_status=alarm_init)
            info = parse_device_response(raw)
            if info:
                found.append(info)
        return found

    def poll_device(self, address: int):
        if address not in self.MOCK_ADDRESSES:
            return None
        lo, hi = self._alarm_seeds[address]
        new_val = self._base_conc[address] + random.uniform(-2.0, 2.0)
        new_val = max(0.0, min(100.0, new_val))
        self._base_conc[address] = new_val
        c = round(new_val, 1)
        alarm = 3 if c >= hi else (2 if c >= lo else 1)
        raw = self._make_fake_response(address, concentration=c, alarm_status=alarm)
        return parse_device_response(raw)

    def close(self):
        return None

    def _make_fake_response(self, address: int, concentration=None, alarm_status: int = 1) -> bytes:
        lo, hi = self._alarm_seeds[address]
        c = concentration if concentration is not None else self._base_conc[address]
        dec, div = 1, 10
        payload = (
            struct.pack(">H", self._gas_types[address]) +
            struct.pack(">H", 0) +
            struct.pack(">H", dec) +
            struct.pack(">I", 100 * div) +
            struct.pack(">I", int(c * div)) +
            struct.pack(">I", int(lo * div)) +
            struct.pack(">I", int(hi * div)) +
            struct.pack(">I", 0) +
            struct.pack(">I", 0) +
            struct.pack(">H", alarm_status) +
            struct.pack(">H", 0x0218)
        )
        header = bytes([address, 0x03, 0x22])
        full = header + payload
        return full + struct.pack("<H", crc16_modbus(full))


class RS485Scanner:
    def __init__(self, port: str, baud: int = 9600, scan_device_count: int = 8):
        self._port = port
        self._baud = baud
        self._scan_device_count = scan_device_count
        self._ser = None

    def open(self):
        self._ser = serial.Serial(
            port=self._port,
            baudrate=self._baud,
            bytesize=8,
            parity="N",
            stopbits=1,
            timeout=SCAN_TIMEOUT,
        )

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()

    def scan(self, on_progress=None):
        found = []
        for addr in range(1, self._scan_device_count + 1):
            if on_progress:
                on_progress(addr, self._scan_device_count)
            cmd = build_scan_command(addr)
            try:
                self._ser.reset_input_buffer()
                logger.debug("Scanning address %d: sending %s", addr, cmd.hex())
                self._ser.write(cmd)
                raw = self._ser.read(39)
                if len(raw) == 39 and raw[0] == addr:
                    info = parse_device_response(raw)
                    if info:
                        found.append(info)
            except Exception:
                continue
        return found

    def poll_device(self, address: int):
        cmd = build_scan_command(address)
        try:
            self._ser.reset_input_buffer()
            self._ser.write(cmd)
            raw = self._ser.read(39)
            if len(raw) == 39 and raw[0] == address:
                return parse_device_response(raw)
        except Exception:
            pass
        return None


@dataclass
class ServiceConfig:
    mode: str
    port: str
    baud: int
    poll_interval: float
    db_write_interval: float
    rescan_interval: float
    scan_device_count: int


class BackgroundPollingService:
    def __init__(self, cfg: ServiceConfig):
        self._cfg = cfg
        self._stop = threading.Event()
        self._scanner = None
        self._plant_id: int | None = None
        self._addresses: list[int] = []
        self._device_id_map: dict[int, int] = {}
        self._next_archive_write_at: dict[int, int] = {}
        self._prev_online_addresses: set[int] = set()  # Track previous scan for offline detection

    def start(self):
        initialise_schema()
        init_alert_db()
        self._plant_id = get_default_plant_id()

        if self._cfg.mode == "mock":
            self._scanner = MockScanner()
        else:
            if not SERIAL_AVAILABLE:
                raise RuntimeError("pyserial is required for real mode")
            self._cfg.scan_device_count = min(255, max(1, self._cfg.scan_device_count))
            self._scanner = RS485Scanner(self._cfg.port, self._cfg.baud, self._cfg.scan_device_count)
            self._scanner.open()

        logger.info("Service started in %s mode", self._cfg.mode.upper())
        logger.info("poll_interval=%ss db_write_interval=%ss rescan_interval=%ss",
                    self._cfg.poll_interval, self._cfg.db_write_interval, self._cfg.rescan_interval)

        self._sync_devices_from_scan()
        poll_interval = max(0.1, float(self._cfg.poll_interval))
        next_poll = time.time()
        next_rescan = time.time() + self._cfg.rescan_interval
        next_report_scheduler_tick = time.time() + 3.0

        try:
            while not self._stop.is_set():
                now = time.time()

                if now >= next_poll:
                    self._poll_once(now)
                    next_poll = now + poll_interval

                if now >= next_rescan:
                    self._sync_devices_from_scan()
                    next_rescan = now + self._cfg.rescan_interval

                if now >= next_report_scheduler_tick:
                    try:
                        sent_count, due_count = run_pending_scheduled_reports(actor="system")
                        if due_count > 0:
                            logger.info(
                                "Scheduled report runner: due=%d sent=%d",
                                int(due_count),
                                int(sent_count),
                            )
                    except Exception:
                        logger.exception("Scheduled report runner failed")
                    finally:
                        next_report_scheduler_tick = now + REPORT_SCHEDULER_TICK_SECONDS

                next_wakeup = min(next_poll, next_rescan, next_report_scheduler_tick)
                wait_for = max(0.0, next_wakeup - time.time())
                self._stop.wait(wait_for)
        finally:
            self._shutdown()

    def stop(self):
        self._stop.set()

    def _sync_devices_from_scan(self):
        if self._plant_id is None:
            return

        found = self._scanner.scan()
        discovered_addresses = set()

        for dev in found:
            addr = dev["address"]
            discovered_addresses.add(addr)
            existing_name = get_device_name(addr)
            dev_name = existing_name if existing_name else f"Device {addr:03d}"

            db_id = upsert_device(
                plant_id=self._plant_id,
                device_address=addr,
                device_name=dev_name,
                gas_type=dev.get("gas_name", ""),
                gas_unit="ppm",
                device_range=dev.get("device_range", 0.0),
                device_status_flag=1,
            )
            self._device_id_map[addr] = db_id

        db_devices = get_all_devices(self._plant_id)

        for db_dev in db_devices:
            addr = db_dev["device_address"]
            self._device_id_map.setdefault(addr, db_dev["id"])

        db_status_by_addr: dict[int, int] = {}
        db_name_by_addr: dict[int, str] = {}
        for db_dev in db_devices:
            addr = int(db_dev.get("device_address", 0) or 0)
            if addr <= 0:
                continue
            db_status_by_addr[addr] = int(db_dev.get("device_status_flag", 0) or 0)
            db_name_by_addr[addr] = str(db_dev.get("device_name") or f"Device {addr:03d}")

        # --- Retry any DB device that did not respond in this scan ---
        db_all_addresses = set(db_status_by_addr.keys())
        missed_in_scan = db_all_addresses - discovered_addresses
        for addr in missed_in_scan:
            dev_name = db_name_by_addr.get(addr, f"Device {addr:03d}")
            recovered = False
            for attempt in range(1, STARTUP_SCAN_POLL_RETRIES + 1):
                logger.debug(
                    "Scan retry %d/%d for device %d (%s)",
                    attempt, STARTUP_SCAN_POLL_RETRIES, addr, dev_name,
                )
                time.sleep(0.1)
                info = self._scanner.poll_device(addr)
                if info:
                    discovered_addresses.add(addr)
                    db_id = upsert_device(
                        plant_id=self._plant_id,
                        device_address=addr,
                        device_name=dev_name,
                        gas_type=info.get("gas_name", ""),
                        gas_unit="ppm",
                        device_range=info.get("device_range", 0.0),
                        device_status_flag=1,
                    )
                    self._device_id_map[addr] = db_id
                    logger.info("Device %d recovered on scan retry %d", addr, attempt)
                    recovered = True
                    break
            if not recovered:
                logger.warning(
                    "Device %d (%s) did not respond after all %d scan attempt(s) ",
                    addr, dev_name, STARTUP_SCAN_POLL_RETRIES + 1,
                )
                try:
                    check_and_fire_offline_alert(addr, dev_name)
                except Exception:
                    logger.exception(
                        "Failed to fire offline alert for device %d", addr
                    )

        previously_online_from_db = {
            addr for addr, status in db_status_by_addr.items() if status == 1
        }

        # Detect devices that just went offline.
        # Includes:
        #   1) previous in-process scan snapshot (normal runtime transition)
        #   2) DB-persisted online flags (service restart / startup transition)
        just_offline = (
            self._prev_online_addresses | previously_online_from_db
        ) - discovered_addresses
        for offline_addr in just_offline:
            dev_name = db_name_by_addr.get(offline_addr, f"Device {offline_addr:03d}")
            try:
                check_and_fire_offline_alert(offline_addr, dev_name)
            except Exception:
                logger.exception("Failed to fire offline alert for device %d", offline_addr)

        # Keep DB online/offline status in sync with latest scan snapshot.
        try:
            apply_scan_status_flags(self._plant_id, discovered_addresses)
        except Exception:
            logger.exception("Failed to apply scan status flags")

        self._addresses = sorted(self._device_id_map.keys())
        self._prev_online_addresses = discovered_addresses.copy()  # Save for next scan offline detection
        logger.info("Device sync complete: scanned=%d tracked=%d",
                    len(discovered_addresses), len(self._addresses))

    def _poll_once(self, now: float):
        interval_secs = max(1, int(round(self._cfg.db_write_interval)))

        for addr in self._addresses:
            info = self._scanner.poll_device(addr)
            if not info:
                continue

            raw_conc = float(info.get("concentration", 0.0) or 0.0)
            try:
                k_factor = float(get_k_factor_for_device_address(int(addr)))
            except Exception:
                k_factor = 1.0
            conc = raw_conc * k_factor
            low_alarm = info.get("low_alarm", 0.0) * k_factor
            high_alarm = info.get("high_alarm", 0.0) * k_factor
            alarm_status = info.get("alarm_status", 0)
            last_update = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            db_id = self._device_id_map.get(addr)
            if not db_id:
                continue

            # Write to live_readings on every poll (1-2s frequency)
            try:
                upsert_live_reading(
                    device_id=db_id,
                    concentration_value=conc,
                    low_alarm=low_alarm,
                    high_alarm=high_alarm,
                    alarm_status=alarm_status,
                    last_updated=last_update,
                )
            except Exception:
                logger.exception("upsert_live_reading failed for addr=%d", addr)

            # Check and fire alerts on every poll
            try:
                check_and_fire(addr, conc)
            except Exception:
                logger.exception("check_and_fire failed for addr=%d", addr)

            # Write to reading_transactions archive on fixed interval boundaries
            # per device to keep consecutive recorded_at values uniform.
            due_epoch = self._next_archive_write_at.get(addr)
            if due_epoch is None:
                now_epoch = int(now)
                due_epoch = ((now_epoch // interval_secs) + 1) * interval_secs
                self._next_archive_write_at[addr] = due_epoch

            now_epoch = int(now)
            if now_epoch < due_epoch:
                continue

            wrote = 0
            next_due = due_epoch
            while next_due <= now_epoch:
                recorded_at = datetime.datetime.fromtimestamp(next_due).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                try:
                    insert_reading(
                        device_id=db_id,
                        concentration_value=conc,
                        low_alarm=low_alarm,
                        high_alarm=high_alarm,
                        alarm_status=alarm_status,
                        recorded_at=recorded_at,
                    )
                except Exception:
                    logger.exception("insert_reading (archive) failed for addr=%d", addr)
                    break

                wrote += 1
                next_due += interval_secs
                self._next_archive_write_at[addr] = next_due

            if wrote > 1:
                logger.debug(
                    "Backfilled %d archive row(s) for addr=%d up to %s",
                    wrote,
                    addr,
                    datetime.datetime.fromtimestamp(next_due - interval_secs).strftime("%Y-%m-%d %H:%M:%S"),
                )

    def _shutdown(self):
        logger.info("Service shutdown: resetting all devices to offline status")
        if self._plant_id is not None:
            try:
                reset_all_devices_offline(self._plant_id)
            except Exception:
                logger.exception("Error while resetting device status flags")

        if self._scanner and hasattr(self._scanner, "close"):
            try:
                self._scanner.close()
            except Exception:
                logger.exception("Error while closing scanner")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="H2 background polling service")
    parser.add_argument(
        "--mode",
        choices=["mock", "real"],
        default="real",
        help="mock=simulate 8 devices, real=use RS485 serial polling",
    )
    parser.add_argument("--port", default="COM1")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--poll-interval", type=float, default=60.0)
    parser.add_argument("--db-write-interval", type=float, default=60.0)
    parser.add_argument("--rescan-interval", type=float, default=600.0)
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--scan-device-count", type=int, default=8)
    return parser


def configure_service_logging(log_level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def main():
    args = build_arg_parser().parse_args()
    configure_service_logging(args.log_level)

    if not _acquire_single_instance_lock():
        logger.error(
            "Another instance of h2_background_service is already running. "
            "Exiting to prevent duplicate writers."
        )
        sys.exit(1)

    cfg = ServiceConfig(
        mode=args.mode,
        port=args.port,
        baud=args.baud,
        poll_interval=args.poll_interval,
        db_write_interval=args.db_write_interval,
        rescan_interval=args.rescan_interval,
        scan_device_count=args.scan_device_count,
    )

    service = BackgroundPollingService(cfg)
    logger.info("Starting background polling service")
    try:
        service.start()
    except KeyboardInterrupt:
        logger.info("Service interrupted by user")
        service.stop()
    except Exception:
        logger.exception("Service terminated with error")
        raise


if __name__ == "__main__":
    main()
