"""The worker agent: runs continuously on a laptop, registers once, then polls the controller for jobs.
Standard library + psutil only, so PyInstaller's onefile build stays small and self-contained."""
import json
import os
import platform
import shutil
import socket
import tempfile
import threading
import time
import traceback

from . import hardware, jobs
from .client import ApiClient
from .common import data_dir, now, read_json, write_json_private

POLL_IDLE_WAIT = 20     # seconds the controller may hold a poll open when there is nothing to do
RETRY_BACKOFF = (2, 5, 10, 20, 30, 60)  # seconds, caps out; used whenever the controller cannot be reached

MODE_SHARE = {  # fraction of logical cores offered, by mode
    "gaming": 0.0,
    "balanced": 0.5,
    "performance": 0.9,
}
MODE_PRIORITY = {"gaming": "idle", "balanced": "below_normal", "performance": "normal"}


class Worker:
    def __init__(self, config_dir=None, work_dir=None, on_status=None):
        self.config_dir = config_dir or data_dir("worker")
        self.work_dir = work_dir or os.path.join(self.config_dir, "work")
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.work_dir, exist_ok=True)
        self.cfg_path = os.path.join(self.config_dir, "config.json")
        self.cfg = read_json(self.cfg_path, {})
        self.cores = jobs.CoreAllocator()
        self.stop_event = threading.Event()
        self.on_status = on_status or (lambda **kw: None)
        self.lock = threading.Lock()
        self.active = {}           # job_id -> dict(thread, cancel, cores, job)
        self._mode_override = None
        self._threads = []

    # ---------------- enrolment ----------------
    def enrolled(self):
        return bool(self.cfg.get("worker_id") and self.cfg.get("worker_key") and self.cfg.get("server"))

    def enroll(self, server, token, name=None, verify_ssl=True):
        c = ApiClient(server, verify_ssl=verify_ssl)
        status, body = c.post("/api/enroll", {"token": token, "name": name or socket.gethostname(),
                                              "hostname": socket.gethostname(), "os": platform.platform()})
        if status != 200:
            raise RuntimeError(body.get("error", "enrollment failed") if isinstance(body, dict) else "enrollment failed")
        self.cfg.update({"server": server, "worker_id": body["worker_id"], "worker_key": body["worker_key"],
                         "name": body["name"], "verify_ssl": verify_ssl, "mode": self.cfg.get("mode", "balanced")})
        write_json_private(self.cfg_path, self.cfg)
        return body["name"]

    def forget(self):
        self.stop_event.set()
        try:
            os.remove(self.cfg_path)
        except OSError:
            pass
        self.cfg = {}

    # ---------------- helpers ----------------
    def _client(self):
        return ApiClient(self.cfg["server"], self.cfg["worker_id"] + "." + self.cfg["worker_key"],
                         verify_ssl=self.cfg.get("verify_ssl", True))

    def mode(self):
        return self._mode_override or self.cfg.get("mode", "balanced")

    def set_mode(self, mode):
        if mode in MODE_SHARE:
            self.cfg["mode"] = mode
            write_json_private(self.cfg_path, self.cfg)

    def allowed_types(self):
        allow_gpu = bool(self.cfg.get("allow_gpu_jobs", True)) and any(
            g["vendor"] == "nvidia" for g in hardware.gpus_cached())
        types = ["cpu_benchmark", "python", "gpu_info", "fetch_file", "store_file"]
        if allow_gpu:
            types.append("gpu_python")
        return types

    def _offer(self):
        logical = hardware.cpu_info()["logical"]
        mode = self.mode()
        frac = MODE_SHARE.get(mode, 0.5)
        # round up for "performance" so a single-core machine can still share one core when asked to;
        # round down for "balanced" so it always keeps at least half the cores for local use
        import math
        share = math.ceil(logical * frac) if mode == "performance" else int(logical * frac)
        with self.lock:
            used = len(self.cores.used)
        free_cores = max(0, share - used)
        ram = hardware.ram_info()
        reserve_mb = int(self.cfg.get("reserve_ram_mb", 1024))
        return {
            "free_cores": 0 if self.mode() == "gaming" else free_cores,
            "gpu_free": self.mode() != "gaming" and not self._gpu_busy(),
            "free_mem_mb": max(0, (ram["available_mb"] or 0) - reserve_mb),
            "allowed_types": [] if self.mode() == "gaming" else self.allowed_types(),
            "tags": self.cfg.get("tags", []),
        }

    def _gpu_busy(self):
        with self.lock:
            return any(a["job"]["requirements"].get("gpu") for a in self.active.values())

    def status_snapshot(self):
        with self.lock:
            running = list(self.active.keys())
        return {"caps": {"cpu": hardware.cpu_info(), "ram": hardware.ram_info(), "gpus": hardware.gpus_cached()},
                "stats": {"cpu_percent": hardware.cpu_percent(), "ram": hardware.ram_info(),
                         "disk": hardware.disk_info(self.work_dir), "gpus": hardware.gpus_cached()},
                "running": running, "mode": self.mode()}

    # ---------------- lifecycle ----------------
    def run_forever(self):
        """Blocks. Call stop() from another thread, or set stop_event, to return."""
        backoff_i = 0
        while not self.stop_event.is_set():
            if not self.enrolled():
                self.on_status(state="not enrolled")
                time.sleep(2)
                continue
            try:
                self._poll_once()
                backoff_i = 0
            except Exception as e:
                self.on_status(state="error", detail=str(e))
                time.sleep(RETRY_BACKOFF[min(backoff_i, len(RETRY_BACKOFF) - 1)])
                backoff_i += 1

    def stop(self):
        self.stop_event.set()
        with self.lock:
            for a in self.active.values():
                a["cancel"].set()

    def _poll_once(self):
        c = self._client()
        snap = self.status_snapshot()
        body = dict(self._offer(), wait=POLL_IDLE_WAIT, **snap)
        status, resp = c.post("/api/worker/poll", body, timeout=POLL_IDLE_WAIT + 10)
        if status == 403:
            self.on_status(state="removed by controller")
            self._run_terminate(c)
            return
        if status != 200:
            raise RuntimeError("poll failed: %s" % (resp.get("error") if isinstance(resp, dict) else status))
        self.on_status(state="connected", detail="%d core(s) offered" % body["free_cores"])
        for cmd in resp.get("commands", []):
            self._handle_command(c, cmd)
        if resp.get("job"):
            self._start_job(c, resp["job"])

    def _handle_command(self, c, cmd):
        t = cmd.get("type")
        if t == "set_mode":
            self.set_mode(cmd.get("mode", "balanced"))
        elif t == "cancel_job":
            with self.lock:
                a = self.active.get(cmd.get("job_id"))
                if a:
                    a["cancel"].set()
        elif t == "terminate":
            self._run_terminate(c)

    def _run_terminate(self, c):
        self.on_status(state="terminating")
        self.stop()
        try:
            c.post("/api/worker/terminated", {})
        except Exception:
            pass
        try:
            from . import uninstall
            uninstall.self_destruct(self.config_dir)
        except Exception:
            pass

    # ---------------- jobs ----------------
    def _start_job(self, c, job):
        need_cores = max(1, int((job.get("requirements") or {}).get("cores", 1)))
        logical = hardware.cpu_info()["logical"]
        share = int(logical * MODE_SHARE.get(self.mode(), 0.5))
        cores = self.cores.take(logical, share, need_cores)
        cancel = threading.Event()
        with self.lock:
            self.active[job["id"]] = {"job": job, "cancel": cancel, "cores": cores}
        t = threading.Thread(target=self._run_job, args=(c, job, cores, cancel), daemon=True)
        self._threads.append(t)
        t.start()

    def _run_job(self, c, job, cores, cancel):
        jid = job["id"]
        job_dir = os.path.join(self.work_dir, jid)
        os.makedirs(job_dir, exist_ok=True)
        result = {"state": "failed", "exit_code": None, "metrics": {}, "log_tail": "", "error": None}
        try:
            if job.get("has_input"):
                zpath = os.path.join(job_dir, "input.zip")
                status, _ = c.get_file("/api/worker/jobs/%s/input" % jid, zpath, timeout=max(60, job["timeout_s"]))
                if status != 200:
                    raise RuntimeError("could not download the job input")
                jobs.safe_extract(zpath, os.path.join(job_dir, "in"))
            result = self._execute(job, job_dir, cores, cancel)
        except Exception as e:
            result = {"state": "failed", "exit_code": None, "metrics": {}, "error": str(e),
                      "log_tail": traceback.format_exc()[-4000:]}
        finally:
            try:
                out_dir = os.path.join(job_dir, "out")
                if os.path.isdir(out_dir) and os.listdir(out_dir):
                    zpath = os.path.join(job_dir, "output.zip")
                    jobs.zip_path(out_dir, zpath)
                    c.put_file("/api/worker/jobs/%s/output" % jid, zpath, timeout=max(60, job["timeout_s"]))
            except Exception as e:
                result.setdefault("log_tail", "")
                result["log_tail"] += "\n[output upload failed: %s]" % e
            try:
                c.post("/api/worker/jobs/%s/result" % jid, result, timeout=60)
            except Exception:
                pass
            self.cores.give_back(cores)
            with self.lock:
                self.active.pop(jid, None)
            try:
                shutil.rmtree(job_dir, ignore_errors=True)
            except OSError:
                pass

    def _execute(self, job, job_dir, cores, cancel):
        spec = job.get("spec") or {}
        priority = MODE_PRIORITY.get(self.mode(), "below_normal")
        mem_limit = (job.get("requirements") or {}).get("memory_mb")
        timeout_s = job.get("timeout_s", 3600)
        jtype = job["type"]

        if jtype == "cpu_benchmark":
            seconds = min(float(spec.get("seconds", 5)), 120)
            r = jobs.cpu_benchmark(seconds, max(1, len(cores) or 1), cancel)
            return {"state": "succeeded", "exit_code": 0, "metrics": r, "log_tail": ""}

        if jtype == "gpu_info":
            return {"state": "succeeded", "exit_code": 0, "metrics": {"gpus": hardware.gpus_cached()}, "log_tail": ""}

        if jtype == "fetch_file":
            rel = spec.get("path", "")
            try:
                src = jobs.safe_join(self.cfg.get("shared_root", job_dir), rel)
            except ValueError as e:
                return {"state": "failed", "error": str(e), "metrics": {}, "log_tail": ""}
            out_dir = os.path.join(job_dir, "out")
            os.makedirs(out_dir, exist_ok=True)
            if not os.path.isfile(src):
                return {"state": "failed", "error": "file not found: %s" % rel, "metrics": {}, "log_tail": ""}
            shutil.copy2(src, os.path.join(out_dir, os.path.basename(src)))
            return {"state": "succeeded", "exit_code": 0, "metrics": {"bytes": os.path.getsize(src)}, "log_tail": ""}

        if jtype == "store_file":
            rel = spec.get("path", "")
            try:
                dest = jobs.safe_join(self.cfg.get("shared_root", job_dir), rel)
            except ValueError as e:
                return {"state": "failed", "error": str(e), "metrics": {}, "log_tail": ""}
            src_dir = os.path.join(job_dir, "in")
            found = next((os.path.join(r, f) for r, _, fs in os.walk(src_dir) for f in fs), None)
            if not found:
                return {"state": "failed", "error": "no input file received", "metrics": {}, "log_tail": ""}
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            shutil.copy2(found, dest)
            return {"state": "succeeded", "exit_code": 0, "metrics": {"bytes": os.path.getsize(found)}, "log_tail": ""}

        if jtype in ("python", "gpu_python"):
            entry = spec.get("entry", "main.py")
            try:
                script = jobs.safe_join(os.path.join(job_dir, "in"), entry)
            except ValueError as e:
                return {"state": "failed", "error": str(e), "metrics": {}, "log_tail": ""}
            if not os.path.isfile(script):
                return {"state": "failed", "error": "entry file not found: %s" % entry, "metrics": {}, "log_tail": ""}
            out_dir = os.path.join(job_dir, "out")
            os.makedirs(out_dir, exist_ok=True)
            env = dict(os.environ, RCLOUD_OUTPUT_DIR=out_dir, RCLOUD_JOB_ID=job["id"])
            if jtype == "python":
                env["CUDA_VISIBLE_DEVICES"] = "-1"
            log_path = os.path.join(job_dir, "log.txt")
            import sys
            cmd = [sys.executable, script] + list(spec.get("args", []))
            last_cp = [0]

            def tick():
                sig = jobs.dir_signature(out_dir)
                h = hash(tuple(sig))
                if h != last_cp[0]:
                    last_cp[0] = h
                    cp_zip = os.path.join(job_dir, "checkpoint.zip")
                    jobs.zip_path(out_dir, cp_zip)
                    self._client().put_file("/api/worker/jobs/%s/checkpoint" % job["id"], cp_zip, timeout=120)

            code, reason = jobs.run_subprocess(cmd, os.path.dirname(script), env, log_path, timeout_s,
                                               mem_limit, cancel, cores, priority, tick=tick)
            tail = jobs.tail_file(log_path)
            if reason == "cancelled":
                return {"state": "failed", "exit_code": code, "error": "cancelled", "log_tail": tail, "metrics": {}}
            if reason:
                return {"state": "failed", "exit_code": code, "error": reason, "log_tail": tail, "metrics": {}}
            ok = code == 0
            return {"state": "succeeded" if ok else "failed", "exit_code": code,
                    "error": None if ok else "exit code %s" % code, "log_tail": tail, "metrics": {}}

        return {"state": "failed", "error": "unknown job type: %s" % jtype, "metrics": {}, "log_tail": ""}
