"""Removes RCloud from this device: the run-at-login entry, local config, and finally itself.
Called when the admin presses Terminate on the dashboard, or the person runs rcloud-worker --uninstall."""
import os
import shutil
import subprocess
import sys
import tempfile
import time

from . import startup


def self_destruct(config_dir, delay_s=2):
    """Removes the run-at-login entry and config now; schedules the program file for deletion after
    this process exits (Windows keeps a running EXE locked, so it cannot delete itself directly)."""
    try:
        startup.uninstall()
    except Exception:
        pass
    try:
        shutil.rmtree(config_dir, ignore_errors=True)
    except OSError:
        pass

    exe = sys.executable if getattr(sys, "frozen", False) else None
    if not exe:
        os._exit(0)

    if sys.platform == "win32":
        bat = os.path.join(tempfile.gettempdir(), "rcloud_remove.bat")
        with open(bat, "w") as f:
            f.write(
                "@echo off\r\n"
                "ping 127.0.0.1 -n %d > nul\r\n"
                ":loop\r\n"
                "del /f /q \"%s\" > nul 2>&1\r\n"
                "if exist \"%s\" goto loop\r\n"
                "del /f /q \"%%~f0\"\r\n" % (delay_s + 1, exe, exe)
            )
        subprocess.Popen(["cmd", "/c", "start", "", "/min", bat],
                         creationflags=0x08000000 | 0x00000008)  # CREATE_NO_WINDOW | DETACHED_PROCESS
    else:
        sh = os.path.join(tempfile.gettempdir(), "rcloud_remove.sh")
        with open(sh, "w") as f:
            f.write("#!/bin/sh\nsleep %d\nrm -f '%s' '$0'\n" % (delay_s, exe))
        os.chmod(sh, 0o700)
        subprocess.Popen(["/bin/sh", sh], start_new_session=True)
    os._exit(0)
