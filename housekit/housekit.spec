# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec: one _internal runtime, two executables.

    HouseKit.exe       windowed GUI (also runs CLI commands when given arguments)
    housekit-cli.exe   console CLI  (a different name: Windows would treat housekit.exe and
                       HouseKit.exe as the same file and one would overwrite the other)

Build with:  .venv\\Scripts\\pyinstaller housekit.spec --noconfirm
Set HOUSEKIT_FFMPEG_DIR to a folder holding ffmpeg.exe + ffprobe.exe to bundle them.
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files

block_cipher = None
here = Path(SPECPATH)

datas = [(str(here / "housekit" / "data"), "housekit/data")]
binaries = []
hiddenimports = ["housekit.features", "housekit.gui", "housekit.cli", "tkinter", "tkinter.ttk", "tkinter.filedialog",
                 "tkinter.messagebox", "tkinter.scrolledtext"]

# pdfplumber -> pdfminer.six + pypdfium2 (ships a DLL); rapidfuzz and numpy are compiled.
for pkg in ("pdfplumber", "pdfminer", "pypdfium2", "pypdfium2_raw", "rapidfuzz", "pyloudnorm"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:
        pass

ffdir = os.environ.get("HOUSEKIT_FFMPEG_DIR")
if ffdir:
    for exe in ("ffmpeg.exe", "ffprobe.exe", "ffmpeg", "ffprobe"):
        p = Path(ffdir) / exe
        if p.exists():
            binaries.append((str(p), "ffmpeg"))

a = Analysis(
    [str(here / "housekit" / "gui_main.py")],
    pathex=[str(here)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "pandas", "PIL", "IPython", "pytest"],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe_gui = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="HouseKit",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)

a_cli = Analysis(
    [str(here / "housekit" / "cli_main.py")],
    pathex=[str(here)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "pandas", "PIL", "IPython", "pytest"],
    cipher=block_cipher,
    noarchive=False,
)
pyz_cli = PYZ(a_cli.pure, a_cli.zipped_data, cipher=block_cipher)
exe_cli = EXE(
    pyz_cli, a_cli.scripts, [],
    exclude_binaries=True,
    name="housekit-cli",
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe_gui, a.binaries, a.zipfiles, a.datas,
    exe_cli, a_cli.binaries, a_cli.zipfiles, a_cli.datas,
    strip=False,
    upx=False,
    name="HouseKit",
)
