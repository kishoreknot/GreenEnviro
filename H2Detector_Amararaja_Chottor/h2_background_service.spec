# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for H2 Background Service.

Creates a standalone .exe with all Python dependencies embedded.
Source code is not visible to end users.

Usage:
    pyinstaller h2_background_service.spec
"""

import sys
from pathlib import Path

block_cipher = None

# Get the directory where this spec file is located
SPEC_DIR = Path(globals().get('SPECPATH', Path.cwd())).resolve()
PROJ_DIR = SPEC_DIR

a = Analysis(
    [str(SPEC_DIR / 'h2_background_service.py')],
    pathex=[str(SPEC_DIR)],
    binaries=[],
    datas=[],
    hiddenimports=[
        # Local modules
        'db_schema',
        'db_repository',
        'db_connection',
        'alert_manager',
        # Serial communication
        'serial',
        'serial.tools',
        'serial.tools.list_ports',
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
    name='H2_BackgroundService',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # Show console window (needed for logging)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
    uac_admin=False,
)
