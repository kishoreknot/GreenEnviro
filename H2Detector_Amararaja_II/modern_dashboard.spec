# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for H2 Dashboard UI executable.

Usage:
    pyinstaller modern_dashboard.spec
"""

from pathlib import Path
import customtkinter as _ctk_mod

block_cipher = None

SPEC_DIR = Path(globals().get('SPECPATH', Path.cwd())).resolve()
_CTK_PATH = Path(_ctk_mod.__file__).parent

a = Analysis(
    [str(SPEC_DIR / 'modern_dashboard.py')],
    pathex=[str(SPEC_DIR)],
    binaries=[],
    datas=[
        (str(SPEC_DIR / 'dark-blue.json'), '.'),
        (str(SPEC_DIR / 'assets'), 'assets'),
        (str(_CTK_PATH), 'customtkinter'),
    ],
    hiddenimports=[
        # UI / charting
        'customtkinter',
        'tkinter',
        'tkinter.ttk',
        'tkinter.messagebox',
        'tkinter.filedialog',
        'matplotlib',
        'matplotlib.backends.backend_tkagg',
        'matplotlib.backends.backend_agg',
        'matplotlib.dates',
        'matplotlib.figure',
        'PIL',
        'PIL._tkinter_finder',
        'PIL.Image',
        'PIL.ImageTk',
        # Data
        'pandas',
        'numpy',
        # PDF export
        'reportlab',
        'reportlab.lib.pagesizes',
        'reportlab.lib.styles',
        'reportlab.lib.enums',
        'reportlab.lib.colors',
        'reportlab.platypus',
        'reportlab.platypus.tables',
        'reportlab.platypus.paragraph',
        # Local modules
        'db_schema',
        'db_repository',
        'db_connection',
        'auth',
        'alert_manager',
        'login_window',
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
    name='H2_Dashboard',
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
    icon=str(SPEC_DIR / 'assets' / 'green-logo.ico'),
)
