# Build:  pyinstaller packaging/rcloud-worker.spec
# Produces a single-file dist/rcloud-worker.exe. No console window (background app with a tray icon).
import sys
block_cipher = None

a = Analysis(
    ['../rcloud/worker_app.py'],
    pathex=['..'],
    hiddenimports=['pystray._win32' if sys.platform == 'win32' else 'pystray._base', 'PIL._tkinter_finder'],
    hookspath=[],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name='rcloud-worker',
    console=False,          # no terminal window; the tray icon is the visible UI
    onefile=True,
    icon=None,
)
