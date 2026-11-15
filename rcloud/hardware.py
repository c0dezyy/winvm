"""Hardware detection. GPU detection works for NVIDIA (nvidia-smi) and, on Windows, lists AMD/Intel adapters.
Utilisation, temperature and compute are only available for NVIDIA in this version."""
import json
import os
import subprocess
import time

try:
    import psutil
except Exception:  # psutil is optional
    psutil = None

NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def cpu_info():
    logical = os.cpu_count() or 1
    physical = psutil.cpu_count(logical=False) if psutil else None
    return {"logical": logical, "physical": physical or logical}


def cpu_percent():
    return psutil.cpu_percent(interval=None) if psutil else None


def ram_info():
    if psutil:
        vm = psutil.virtual_memory()
        return {"total_mb": int(vm.total / 2 ** 20), "available_mb": int(vm.available / 2 ** 20)}
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                info[k] = int(v.split()[0])
        return {"total_mb": info["MemTotal"] // 1024, "available_mb": info.get("MemAvailable", 0) // 1024}
    except Exception:
        return {"total_mb": None, "available_mb": None}


def disk_info(path):
    import shutil
    try:
        os.makedirs(path, exist_ok=True)
        u = shutil.disk_usage(path)
        return {"total_gb": round(u.total / 2 ** 30, 1), "free_gb": round(u.free / 2 ** 30, 1)}
    except Exception:
        return {"total_gb": None, "free_gb": None}


def vendor_of(name, maker=""):
    text = (name + " " + (maker or "")).lower()
    if "nvidia" in text:
        return "nvidia"
    if "amd" in text or "radeon" in text or "advanced micro" in text:
        return "amd"
    if "intel" in text:
        return "intel"
    return "unknown"


def parse_nvidia_smi(text):
    gpus = []
    for line in text.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 7:
            continue

        def num(x):
            try:
                return float(x)
            except ValueError:
                return None
        gpus.append({
            "vendor": "nvidia", "index": int(parts[0]), "name": parts[1], "driver": parts[2],
            "vram_mb": num(parts[3]), "vram_used_mb": num(parts[4]),
            "util_pct": num(parts[5]), "temp_c": num(parts[6]),
            "compute": "cuda (script jobs)",
        })
    return gpus


def parse_windows_video(json_text):
    if not json_text or not json_text.strip():
        return []
    data = json.loads(json_text)
    if isinstance(data, dict):
        data = [data]
    gpus = []
    for i, d in enumerate(data):
        name = d.get("Name") or "Unknown adapter"
        vendor = vendor_of(name, d.get("AdapterCompatibility", ""))
        ram = d.get("AdapterRAM")
        gpus.append({
            "vendor": vendor, "index": i, "name": name, "driver": d.get("DriverVersion"),
            # WMI reports at most 4 GB here, so treat this number as approximate
            "vram_mb": int(ram / 2 ** 20) if isinstance(ram, (int, float)) and ram > 0 else None,
            "util_pct": None, "temp_c": None, "vram_used_mb": None,
            "compute": "detect-only" if vendor != "nvidia" else "cuda (script jobs)",
        })
    return gpus


def _run(cmd, timeout=8):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def detect_gpus(runner=_run):
    smi = parse_nvidia_smi(runner(["nvidia-smi",
        "--query-gpu=index,name,driver_version,memory.total,memory.used,utilization.gpu,temperature.gpu",
        "--format=csv,noheader,nounits"]))
    others = []
    if os.name == "nt":
        out = runner(["powershell", "-NoProfile", "-Command",
                      "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,DriverVersion,AdapterRAM | ConvertTo-Json -Compress"], timeout=15)
        try:
            for g in parse_windows_video(out):
                if g["vendor"] == "nvidia" and smi:
                    continue   # already listed with live numbers
                others.append(g)
        except Exception:
            pass
    gpus = smi + others
    for i, g in enumerate(gpus):
        g["index"] = g.get("index", i) if g["vendor"] == "nvidia" else i
    return gpus


_gpu_cache = {"t": 0, "v": []}


def gpus_cached(max_age=5):
    if time.time() - _gpu_cache["t"] > max_age:
        _gpu_cache["v"] = detect_gpus()
        _gpu_cache["t"] = time.time()
    return _gpu_cache["v"]


def snapshot(work_dir):
    return {"cpu": dict(cpu_info(), percent=cpu_percent()), "ram": ram_info(),
            "disk": disk_info(work_dir), "gpus": gpus_cached()}
