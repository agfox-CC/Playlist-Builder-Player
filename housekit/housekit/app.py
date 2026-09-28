"""Portable entry point. `HouseKit.exe` opens the GUI; `HouseKit.exe <command> ...` runs the CLI.

Absolute imports only: the packaged executables start from tiny wrapper scripts
(gui_main.py / cli_main.py) that import this module, and a script run directly cannot use
package-relative imports.
"""
from __future__ import annotations

import multiprocessing
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

from housekit.config import app_dir, is_frozen


def setup_portable_paths() -> None:
    """Make the bundled ffmpeg visible and run from the app folder so relative config paths resolve.
    Works for both portable layouts: a PyInstaller exe (frozen) and a folder carrying its own
    python\\ interpreter next to the housekit\\ package."""
    root = app_dir()
    cands = [root / "ffmpeg", root]
    if is_frozen():
        bundle = Path(getattr(sys, "_MEIPASS", root))
        cands += [bundle / "ffmpeg", bundle]
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    for cand in cands:
        if (cand / exe).exists():
            os.environ["PATH"] = str(cand) + os.pathsep + os.environ.get("PATH", "")
            break
    if is_frozen() or (root / "housekit.toml").exists() or (root / "python").is_dir():
        try:
            os.chdir(root)
        except OSError:
            pass
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")


def _install_gui_crash_log() -> None:
    """A windowed exe has no console: write uncaught errors to a log next to the app and say so."""
    log_path = app_dir() / "housekit-error.log"

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n[{datetime.now().isoformat(timespec='seconds')}]\n{text}")
        except OSError:
            pass
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("HouseKit", f"Something went wrong:\n\n{exc}\n\nDetails were written to\n{log_path}")
            root.destroy()
        except Exception:
            pass

    sys.excepthook = hook


def main() -> None:
    multiprocessing.freeze_support()
    setup_portable_paths()
    args = sys.argv[1:]
    if args == ["--gui-selftest"]:
        _install_gui_crash_log()
        from housekit.gui import selftest

        selftest()
        return
    if args:
        from housekit.cli import app

        app()
        return
    _install_gui_crash_log()
    from housekit.gui import run

    run()


def cli_main() -> None:
    """Console entry (`housekit-cli.exe`): always the CLI, help when no arguments."""
    multiprocessing.freeze_support()
    setup_portable_paths()
    from housekit.cli import app

    app()


if __name__ == "__main__":
    main()
