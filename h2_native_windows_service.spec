# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

block_cipher = None

SPEC_DIR = Path(globals().get("SPECPATH", Path.cwd())).resolve()

a = Analysis(
    [str(SPEC_DIR / "h2_native_windows_service.py")],
    pathex=[str(SPEC_DIR)],
    binaries=[],
    datas=[],
    hiddenimports=[
        "db_schema",
        "db_repository",
        "db_connection",
        "alert_manager",
        "serial",
        "serial.tools",
        "serial.tools.list_ports",
        "servicemanager",
        "win32service",
        "win32serviceutil",
        "win32event",
        "win32timezone",
        "pywintypes",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="H2_BackgroundWindowsService",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    uac_admin=False,
)