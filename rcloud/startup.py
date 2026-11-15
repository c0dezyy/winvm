"""Run-at-login registration. No hidden process names, no anti-virus evasion, no privilege
escalation: this only makes a background agent start automatically, the same way Dropbox,
OneDrive or a printer helper does. A visible tray icon and an uninstaller are always included."""
import os
import plistlib
import subprocess
import sys

APP_NAME = "RCloudWorker"


def _run_target():
    """The command that should be launched at login: prefer the frozen EXE, fall back to python -m."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--tray"]
    return [sys.executable, "-m", "rcloud.worker_app", "--tray"]


def install_windows():
    import winreg
    cmd = subprocess.list2cmdline(_run_target())
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_SET_VALUE)
    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
    winreg.CloseKey(key)
    return cmd


def uninstall_windows():
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(key, APP_NAME)
        winreg.CloseKey(key)
    except FileNotFoundError:
        pass


def install_macos():
    target = _run_target()
    plist = {"Label": "com." + APP_NAME.lower(), "ProgramArguments": target, "RunAtLoad": True,
             "KeepAlive": {"SuccessfulExit": False}}
    path = os.path.expanduser("~/Library/LaunchAgents/com.%s.plist" % APP_NAME.lower())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    subprocess.run(["launchctl", "load", path], check=False)
    return path


def uninstall_macos():
    path = os.path.expanduser("~/Library/LaunchAgents/com.%s.plist" % APP_NAME.lower())
    subprocess.run(["launchctl", "unload", path], check=False)
    try:
        os.remove(path)
    except OSError:
        pass


def install_linux():
    target = _run_target()
    unit = ("[Unit]\nDescription=RCloud worker (shares spare CPU/GPU/storage)\n\n"
            "[Service]\nExecStart=%s\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n"
            % subprocess.list2cmdline(target))
    d = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "%s.service" % APP_NAME)
    with open(path, "w") as f:
        f.write(unit)
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    subprocess.run(["systemctl", "--user", "enable", "--now", APP_NAME + ".service"], check=False)
    return path


def uninstall_linux():
    subprocess.run(["systemctl", "--user", "disable", "--now", APP_NAME + ".service"], check=False)
    path = os.path.expanduser("~/.config/systemd/user/%s.service" % APP_NAME)
    try:
        os.remove(path)
    except OSError:
        pass


def install():
    if sys.platform == "win32":
        return install_windows()
    if sys.platform == "darwin":
        return install_macos()
    return install_linux()


def uninstall():
    if sys.platform == "win32":
        uninstall_windows()
    elif sys.platform == "darwin":
        uninstall_macos()
    else:
        uninstall_linux()
