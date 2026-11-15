"""Job helpers and executors. Limits are enforced by process priority, CPU affinity, a memory watchdog and a timeout.
They are NOT a security sandbox: only enable script jobs on a laptop for jobs you trust."""
import hashlib
import os
import shutil
import signal
import subprocess
import threading
import time
import zipfile

try:
    import psutil
except Exception:
    psutil = None


# ---------------- zip + path safety ----------------
def safe_join(base, rel):
    base = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base, rel))
    if target != base and not target.startswith(base + os.sep):
        raise ValueError("path escapes the allowed folder: %r" % rel)
    return target


def safe_extract(zip_path, dest, max_files=20000, max_bytes=2 * 2 ** 30):
    os.makedirs(dest, exist_ok=True)
    with zipfile.ZipFile(zip_path) as z:
        infos = z.infolist()
        if len(infos) > max_files:
            raise ValueError("zip has too many files")
        total = 0
        for info in infos:
            target = safe_join(dest, info.filename)
            total += info.file_size
            if total > max_bytes:
                raise ValueError("zip is too large when extracted")
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)


def zip_path(src, dest_zip, extra_files=None):
    """Zips a file or folder. extra_files maps a name inside the zip to a path on disk."""
    count = 0
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.isdir(src):
            for root, _, files in os.walk(src):
                for name in files:
                    full = os.path.join(root, name)
                    z.write(full, os.path.relpath(full, src))
                    count += 1
        elif os.path.isfile(src):
            z.write(src, os.path.basename(src))
            count += 1
        for arc, full in (extra_files or {}).items():
            if os.path.isfile(full):
                z.write(full, arc)
                count += 1
    return count


def dir_signature(path):
    sig = []
    for root, _, files in os.walk(path):
        for name in files:
            full = os.path.join(root, name)
            try:
                st = os.stat(full)
                sig.append((os.path.relpath(full, path), st.st_size, st.st_mtime_ns))
            except OSError:
                pass
    return sorted(sig)


def tail_file(path, n=20000):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read().decode("utf-8", "replace")
    except OSError:
        return ""


# ---------------- CPU allocation ----------------
class CoreAllocator:
    """Hands out distinct logical cores from the pool this laptop shares (the highest-numbered cores,
    so core 0 stays free for local apps)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.used = set()

    def pool(self, logical, share):
        return list(range(max(0, logical - share), logical))

    def free_count(self, logical, share):
        with self.lock:
            return len([c for c in self.pool(logical, share) if c not in self.used])

    def take(self, logical, share, n):
        with self.lock:
            free = [c for c in self.pool(logical, share) if c not in self.used]
            if len(free) < n:
                return None
            chosen = free[:n]
            self.used.update(chosen)
            return chosen

    def give_back(self, cores):
        with self.lock:
            self.used.difference_update(cores or [])


def apply_limits(pid, cores, priority):
    if not psutil:
        return
    try:
        p = psutil.Process(pid)
    except Exception:
        return
    try:
        if cores and hasattr(p, "cpu_affinity"):
            p.cpu_affinity(cores)
    except Exception:
        pass
    try:
        if os.name == "nt":
            classes = {"idle": getattr(psutil, "IDLE_PRIORITY_CLASS", None),
                       "below_normal": getattr(psutil, "BELOW_NORMAL_PRIORITY_CLASS", None),
                       "normal": getattr(psutil, "NORMAL_PRIORITY_CLASS", None)}
            if classes.get(priority) is not None:
                p.nice(classes[priority])
        else:
            p.nice({"idle": 19, "below_normal": 10, "normal": 0}.get(priority, 0))
    except Exception:
        pass


def tree_rss_mb(pid):
    if not psutil:
        return 0.0
    try:
        p = psutil.Process(pid)
        procs = [p] + p.children(recursive=True)
        return sum(x.memory_info().rss for x in procs if x.is_running()) / 2 ** 20
    except Exception:
        return 0.0


def kill_tree(proc):
    try:
        if psutil:
            parent = psutil.Process(proc.pid)
            for c in parent.children(recursive=True):
                try:
                    c.kill()
                except Exception:
                    pass
            parent.kill()
        elif os.name != "nt":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception:
        pass


def run_subprocess(cmd, cwd, env, log_path, timeout_s, mem_limit_mb, cancel, cores, priority,
                   tick=None, tick_every=15.0):
    """Runs a process under limits. Returns (exit_code, reason) with reason in None, timeout, memory limit, cancelled."""
    flags = 0
    kwargs = {}
    if os.name == "nt":
        flags = 0x00000200 | 0x08000000   # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags, **kwargs)
        apply_limits(proc.pid, cores, priority)
        started = time.time()
        last_tick = started
        reason = None
        while proc.poll() is None:
            time.sleep(0.2)
            if cancel.is_set():
                reason = "cancelled"
            elif time.time() - started > timeout_s:
                reason = "timeout"
            elif mem_limit_mb and tree_rss_mb(proc.pid) > mem_limit_mb:
                reason = "memory limit"
            if reason:
                kill_tree(proc)
                break
            if tick and time.time() - last_tick >= tick_every:
                last_tick = time.time()
                try:
                    tick()
                except Exception:
                    pass
        proc.wait()
        return proc.returncode, reason


# ---------------- built-in jobs ----------------
def cpu_benchmark(seconds, threads, cancel):
    """SHA-256 over a 1 MB buffer on N threads (hashlib releases the GIL, so threads use separate cores)."""
    buf = os.urandom(1 << 20)
    counts = [0] * threads
    end = time.time() + seconds

    def work(i):
        n = 0
        while time.time() < end and not cancel.is_set():
            hashlib.sha256(buf).digest()
            n += 1
        counts[i] = n

    started = time.time()
    ts = [threading.Thread(target=work, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    elapsed = max(time.time() - started, 1e-6)
    total_mb = sum(counts)
    return {"threads": threads, "seconds": round(elapsed, 2), "mb_hashed": total_mb, "mb_per_s": round(total_mb / elapsed, 1)}
