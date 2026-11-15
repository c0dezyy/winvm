"""Small shared helpers (standard library only)."""
import json
import os
import re
import tempfile
import time


def now():
    return time.time()


def data_dir(role, override=None):
    if override:
        return override
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "rcloud", role)


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_name(value, maxlen=40):
    """Device names: no control characters, collapsed spaces, at most 40 characters."""
    text = " ".join(_CTRL.sub(" ", str(value or "")).split())
    if not text:
        raise ValueError("name is empty")
    return text[:maxlen]


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json_private(path, obj):
    """Atomic write, owner-only permissions where the OS supports it."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)
