# build.spec
import customtkinter
import os

ctk_path = os.path.dirname(customtkinter.__file__)

a = Analysis(
    ['modern_dashboard.py'],
    pathex=[],
    binaries=[],
    datas=[
        (ctk_path, 'customtkinter'),      # CTk themes + assets
    ],
    hiddenimports=[
        'serial',
        'serial.tools.list_ports',
        'matplotlib.backends.backend_tkagg',
        'PIL._tkinter_finder',
        'reportlab',
        'reportlab.lib',
        'reportlab.lib.pagesizes',
        'reportlab.lib.styles',
        'reportlab.lib.colors',
        'reportlab.platypus',
        'reportlab.platypus.tables',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)  # see Step 3 for cipher

exe = EXE(
    pyz, a.scripts, a.binaries, a.datas,
    name='H2GasDetector',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,                             # compress — requires UPX on PATH
    console=False,                        # no black terminal window
    onefile=True,                         # single .exe
    icon='assets/green-logo.ico',              # optional — your app icon
)