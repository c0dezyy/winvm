"""Controller: keeps the registry of laptops (workers), the job queue and the blob store.
It runs on an always-on machine, independent of any temporary GitHub runner. Standard library only."""
import json
import os
import re
import secrets
import shutil
import ssl
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from . import security as sec
from .common import ApiError, clean_name, now
from .store import Store

JOB_TYPES = ("cpu_benchmark", "python", "gpu_python", "gpu_info", "fetch_file", "store_file")
MODES = ("gaming", "balanced", "performance")


class Controller:
    def __init__(self, data_dir, offline_after=30.0, max_blob_mb=512, cors_origins=()):
        os.makedirs(data_dir, exist_ok=True)
        self.data_dir = data_dir
        self.store = Store(os.path.join(data_dir, "controller.db"))
        self.blobs = os.path.join(data_dir, "blobs")
        os.makedirs(self.blobs, exist_ok=True)
        self.offline_after = offline_after
        self.max_blob = max_blob_mb * 2 ** 20
        self.cors_origins = tuple(cors_origins)
        self.cond = threading.Condition()
        self.sessions = {}
        self.limiter = sec.RateLimiter(max_fail=5, window_s=60, lock_s=60)
        self.stop_event = threading.Event()
        self._thread = None

    # ---------------- setup ----------------
    def init_admin(self, password=None):
        """Creates the admin password on first run. Returns the password once, or None if it already exists."""
        if self.store.get_setting("admin_hash"):
            return None
        password = password or secrets.token_urlsafe(12)
        self.store.set_setting("admin_hash", sec.hash_password(password))
        return password

    def start(self):
        self._thread = threading.Thread(target=self._reaper, daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_event.set()
        self.notify()

    def notify(self):
        with self.cond:
            self.cond.notify_all()

    # ---------------- auth ----------------
    def admin_login(self, password, ip):
        if self.limiter.blocked(ip):
            raise ApiError(429, "too many failed attempts, wait a minute")
        stored = self.store.get_setting("admin_hash")
        if not stored or not sec.verify_password(password or "", stored):
            self.limiter.failure(ip)
            raise ApiError(401, "wrong password")
        self.limiter.success(ip)
        token = sec.new_token("ad")
        self.sessions[sec.hash_token(token)] = now() + 24 * 3600
        return token

    def is_admin(self, token):
        exp = self.sessions.get(sec.hash_token(token))
        return bool(exp and exp > now())

    def logout(self, token):
        self.sessions.pop(sec.hash_token(token), None)

    def who(self, bearer):
        """Returns (role, record) for an Authorization bearer token."""
        if not bearer:
            raise ApiError(401, "missing token")
        if bearer.startswith("ad_"):
            if self.is_admin(bearer):
                return "admin", None
            raise ApiError(401, "session expired")
        if bearer.startswith("cl_"):
            row = self.store.find_token("client", bearer)
            if row:
                return "client", row
            raise ApiError(401, "invalid client token")
        w = self.store.auth_worker(bearer)
        if w:
            if w["revoked"]:
                raise ApiError(403, "this device was removed")
            return "worker", w
        raise ApiError(401, "invalid token")

    # ---------------- enrolment ----------------
    def enroll(self, body, ip):
        if self.limiter.blocked(ip):
            raise ApiError(429, "too many failed attempts, wait a minute")
        row = self.store.use_enroll_token(str(body.get("token", "")))
        if not row:
            self.limiter.failure(ip)
            raise ApiError(401, "invalid, used up or expired enrollment token")
        self.limiter.success(ip)
        try:
            name = clean_name(body.get("name") or body.get("hostname") or "laptop")
        except ValueError:
            name = "laptop"
        mode = body.get("mode") if body.get("mode") in MODES else "balanced"
        wid, key = self.store.create_worker(name, str(body.get("hostname", ""))[:80], str(body.get("os", ""))[:80],
                                            body.get("caps") or {}, body.get("policy") or {}, mode)
        self.store.event("enrolled", "%s (%s)" % (name, wid))
        return {"worker_id": wid, "worker_key": key, "name": name}

    # ---------------- worker side ----------------
    def worker_poll(self, worker, body):
        wait = min(max(float(body.get("wait", 0) or 0), 0.0), 25.0)
        self._touch(worker, body)
        deadline = time.time() + wait
        offer = {
            "free_cores": int(body.get("free_cores", 0) or 0),
            "gpu_free": bool(body.get("gpu_free", False)),
            "free_mem_mb": body.get("free_mem_mb"),
            "allowed_types": list(body.get("allowed_types", [])),
            "tags": list(body.get("tags", [])),
        }
        cmds, job = [], None
        while True:
            cmds = self.store.take_commands(worker["id"])
            w = self.store.get_worker(worker["id"])
            if not cmds and not w["paused"] and not w["terminate_state"] and offer["free_cores"] > 0:
                job = self.store.claim_job(w, offer)
            if cmds or job or time.time() >= deadline or self.stop_event.is_set():
                break
            with self.cond:
                self.cond.wait(timeout=max(0.05, min(1.0, deadline - time.time())))
        self.store.update("workers", worker["id"], last_seen=now())
        out = {"commands": cmds, "job": None, "server_time": now()}
        if job:
            out["job"] = {"id": job["id"], "type": job["type"], "spec": job["spec"], "requirements": job["requirements"],
                          "timeout_s": job["timeout_s"], "has_input": bool(job["has_input"]),
                          "checkpoint_version": job["checkpoint_version"], "attempt": job["attempts"]}
        return out

    def _touch(self, worker, body):
        w = self.store.get_worker(worker["id"])
        upd = {"last_seen": now()}
        if not w["online_since"]:
            upd["online_since"] = now()
            upd["offline_since"] = None
            self.store.event("online", "%s (%s)" % (w["name"], w["id"]))
        for field in ("caps", "stats", "policy"):
            if isinstance(body.get(field), dict):
                upd[field] = body[field]
        if body.get("mode") in MODES:
            upd["mode"] = body["mode"]
        running = body.get("running")
        if isinstance(running, list):
            upd["running"] = [str(r) for r in running][:100]
        self.store.update("workers", worker["id"], **upd)
        if isinstance(running, list):
            # a job the controller thinks is running here, but the worker does not have, was lost (restart / crash)
            for j in self.store.all("SELECT * FROM jobs WHERE state='running' AND worker_id=?", (worker["id"],)):
                if j["id"] not in running and now() - (j["assigned_at"] or 0) > 15:
                    self.store.requeue_worker_jobs(worker["id"], "worker lost the job", only_ids={j["id"]})
                    self.notify()

    def worker_terminated(self, worker):
        self.store.update("workers", worker["id"], terminate_state="done", revoked=1, online_since=None,
                          offline_since=now())
        self.store.requeue_worker_jobs(worker["id"], "device was removed")
        self.store.event("terminated", "%s (%s)" % (worker["name"], worker["id"]))
        self.notify()

    def worker_result(self, worker, job_id, body):
        job = self.store.get_job(job_id)
        if not job or job["worker_id"] != worker["id"] or job["state"] != "running":
            raise ApiError(409, "job is not running on this device")
        state = "succeeded" if body.get("state") == "succeeded" else "failed"
        result = {"exit_code": body.get("exit_code"), "metrics": body.get("metrics") or {},
                  "log_tail": str(body.get("log_tail", ""))[-20000:]}
        self.store.update("jobs", job_id, state=state, finished=now(), result=result,
                          error=str(body.get("error"))[:500] if body.get("error") else None)
        self.store.event("job_" + state, "%s on %s" % (job_id, worker["name"]))
        return {"ok": True}

    # ---------------- job side ----------------
    def blob_path(self, job_id, kind):
        if not re.fullmatch(r"j[0-9a-f]{10}", job_id) or kind not in ("input", "output", "checkpoint"):
            raise ApiError(400, "bad job id")
        d = os.path.join(self.blobs, job_id)
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, kind + ".zip")

    def submit_job(self, client_name, body):
        jtype = body.get("type")
        if jtype not in JOB_TYPES:
            raise ApiError(400, "unknown job type, use one of: " + ", ".join(JOB_TYPES))
        spec = {k: v for k, v in body.items() if k in ("entry", "args", "seconds", "path", "dir", "threads")}
        if len(json.dumps(spec)) > 65536:
            raise ApiError(400, "job spec too large")
        if "args" in spec and not (isinstance(spec["args"], list) and all(isinstance(a, str) for a in spec["args"])):
            raise ApiError(400, "args must be a list of strings")
        req_in = body.get("requirements") or {}
        req = {"cores": max(1, min(int(req_in.get("cores", 1)), 256))}
        if req_in.get("memory_mb"):
            req["memory_mb"] = max(16, int(req_in["memory_mb"]))
        if req_in.get("gpu") or jtype == "gpu_python":
            req["gpu"] = True
        if req_in.get("tags"):
            req["tags"] = [str(t) for t in req_in["tags"]][:10]
        if req_in.get("worker"):
            req["worker"] = str(req_in["worker"])[:60]
        timeout_s = max(5, min(int(body.get("timeout_s", 3600)), 86400))
        attempts = max(1, min(int(body.get("max_attempts", 3)), 10))
        try:
            name = clean_name(body.get("name") or jtype, 60)
        except ValueError:
            name = jtype
        jid = self.store.create_job(client_name, name, jtype, spec, req, timeout_s, attempts, bool(body.get("input")))
        self.store.event("job_created", "%s (%s)" % (jid, jtype))
        self.notify()
        return jid

    def start_job(self, job_id):
        job = self.store.get_job(job_id)
        if not job:
            raise ApiError(404, "no such job")
        if job["state"] != "staged":
            raise ApiError(409, "job is not waiting for input")
        if not os.path.exists(self.blob_path(job_id, "input")):
            raise ApiError(409, "upload the input first")
        self.store.update("jobs", job_id, state="queued")
        self.notify()

    def cancel_job(self, job_id):
        job = self.store.get_job(job_id)
        if not job:
            raise ApiError(404, "no such job")
        if job["state"] in ("succeeded", "failed", "cancelled"):
            return
        self.store.update("jobs", job_id, state="cancelled", finished=now(), error="cancelled")
        if job["worker_id"]:
            self.store.push_command(job["worker_id"], {"type": "cancel_job", "job_id": job_id})
        self.notify()

    def job_public(self, job):
        return {"id": job["id"], "name": job["name"], "type": job["type"], "state": job["state"],
                "worker_id": job["worker_id"], "created": job["created"], "started": job["started"],
                "finished": job["finished"], "attempts": job["attempts"], "max_attempts": job["max_attempts"],
                "error": job["error"], "result": job["result"], "has_output": bool(job["has_output"]),
                "requirements": job["requirements"]}

    # ---------------- admin side ----------------
    def _status(self, w):
        if w["terminate_state"] == "requested":
            return "terminating"
        if not w["online_since"]:
            return "offline"
        if w["paused"]:
            return "paused"
        return "busy" if w["running"] else "available"

    def public_worker(self, w):
        t = now()
        return {"id": w["id"], "name": w["name"], "hostname": w["hostname"], "os": w["os"],
                "status": self._status(w), "online_since": w["online_since"], "offline_since": w["offline_since"],
                "last_seen": w["last_seen"], "created": w["created"], "paused": bool(w["paused"]),
                "mode": w["mode"], "caps": w["caps"], "stats": w["stats"], "policy": w["policy"],
                "running": w["running"], "uptime_s": (t - w["online_since"]) if w["online_since"] else None}

    def admin_state(self):
        workers = [self.public_worker(w) for w in self.store.list_workers() if w["terminate_state"] != "done"]
        totals = {"devices": len(workers), "online": 0, "threads": 0, "ram_total_mb": 0, "ram_free_mb": 0,
                  "vram_total_mb": 0, "disk_free_gb": 0.0, "gpus": 0}
        for w in workers:
            if w["status"] in ("offline", "terminating") and not w["online_since"]:
                continue
            totals["online"] += 1
            caps, st = w["caps"] or {}, w["stats"] or {}
            totals["threads"] += int((caps.get("cpu") or {}).get("logical") or 0)
            totals["ram_total_mb"] += int((caps.get("ram") or {}).get("total_mb") or 0)
            totals["ram_free_mb"] += int((st.get("ram") or {}).get("available_mb") or 0)
            totals["disk_free_gb"] += float((st.get("disk") or {}).get("free_gb") or 0)
            for g in (st.get("gpus") or caps.get("gpus") or []):
                totals["gpus"] += 1
                totals["vram_total_mb"] += int(g.get("vram_mb") or 0)
        totals["disk_free_gb"] = round(totals["disk_free_gb"], 1)
        return {"server_time": now(), "workers": workers, "totals": totals,
                "job_counts": self.store.job_counts(),
                "jobs": [self.job_public(j) for j in self.store.recent_jobs(40)],
                "events": self.store.events(20)}

    def admin_worker_action(self, wid, action, body):
        w = self.store.get_worker(wid)
        if not w:
            raise ApiError(404, "no such device")
        if action == "rename":
            try:
                name = clean_name(body.get("name"))
            except ValueError as e:
                raise ApiError(400, str(e))
            self.store.update("workers", wid, name=name)
            self.store.event("renamed", "%s -> %s" % (w["name"], name))
        elif action == "pause":
            self.store.update("workers", wid, paused=1)
        elif action == "resume":
            self.store.update("workers", wid, paused=0)
        elif action == "mode":
            mode = body.get("mode")
            if mode not in MODES:
                raise ApiError(400, "mode must be one of: " + ", ".join(MODES))
            self.store.update("workers", wid, mode=mode)
            self.store.push_command(wid, {"type": "set_mode", "mode": mode})
        elif action == "terminate":
            if body.get("confirm") != w["name"]:
                raise ApiError(400, "type the device name to confirm")
            self.store.update("workers", wid, terminate_state="requested", paused=1)
            self.store.push_command(wid, {"type": "terminate"})
            self.store.event("terminate_requested", "%s (%s)" % (w["name"], wid))
        elif action == "forget":
            self.store.requeue_worker_jobs(wid, "device was forgotten")
            self.store.run("DELETE FROM workers WHERE id=?", (wid,))
        else:
            raise ApiError(404, "unknown action")
        self.notify()

    # ---------------- background ----------------
    def reap_once(self):
        t = now()
        for w in self.store.list_workers():
            if w["online_since"] and (w["last_seen"] or 0) < t - self.offline_after:
                self.store.update("workers", w["id"], online_since=None, offline_since=w["last_seen"])
                moved = self.store.requeue_worker_jobs(w["id"], "worker went offline")
                self.store.event("offline", "%s (%s)%s" % (w["name"], w["id"], ", %d job(s) re-queued" % len(moved) if moved else ""))
                self.notify()
        for k in [k for k, exp in self.sessions.items() if exp < t]:
            self.sessions.pop(k, None)

    def _reaper(self):
        while not self.stop_event.wait(min(2.0, max(0.2, self.offline_after / 4))):
            try:
                self.reap_once()
            except Exception:
                pass


# ============================== HTTP ==============================
def make_handler(ctrl, dashboard_path):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "rcloud"

        def log_message(self, fmt, *args):
            pass

        # ---- helpers ----
        def _cors(self):
            origin = self.headers.get("Origin")
            if origin and ("*" in ctrl.cors_origins or origin in ctrl.cors_origins):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")

        def send_json(self, obj, status=200):
            data = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self._cors()
            self.end_headers()
            self.wfile.write(data)

        def body_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1_000_000:
                raise ApiError(413, "body too large")
            raw = self.rfile.read(n) if n else b""
            if not raw:
                return {}
            try:
                data = json.loads(raw)
            except ValueError:
                raise ApiError(400, "invalid JSON")
            if not isinstance(data, dict):
                raise ApiError(400, "JSON object expected")
            return data

        def save_blob(self, path):
            n = int(self.headers.get("Content-Length") or -1)
            if n < 0:
                raise ApiError(411, "Content-Length required")
            if n > ctrl.max_blob:
                raise ApiError(413, "file too large")
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
            try:
                with os.fdopen(fd, "wb") as f:
                    left = n
                    while left > 0:
                        chunk = self.rfile.read(min(65536, left))
                        if not chunk:
                            raise ApiError(400, "upload cut short")
                        f.write(chunk)
                        left -= len(chunk)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

        def send_blob(self, path):
            if not os.path.exists(path):
                raise ApiError(404, "not found")
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(size))
            self._cors()
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 65536)

        def bearer(self):
            h = self.headers.get("Authorization", "")
            return h[7:].strip() if h.lower().startswith("bearer ") else ""

        def ip(self):
            return self.client_address[0]

        # ---- dispatch ----
        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self._cors()
            self.end_headers()

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _dispatch(self, method):
            try:
                self._route(method, urlsplit(self.path).path)
            except ApiError as e:
                try:
                    self.send_json({"error": e.message}, e.status)
                except Exception:
                    pass
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # never leak internals
                try:
                    self.send_json({"error": "server error"}, 500)
                except Exception:
                    pass

        def _route(self, method, path):
            m = None
            # ---- public ----
            if method == "GET" and path in ("/", "/index.html"):
                with open(dashboard_path, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if method == "GET" and path == "/healthz":
                return self.send_json({"ok": True})
            if method == "POST" and path == "/api/enroll":
                return self.send_json(ctrl.enroll(self.body_json(), self.ip()))
            if method == "POST" and path == "/api/admin/login":
                token = ctrl.admin_login(self.body_json().get("password"), self.ip())
                return self.send_json({"token": token, "expires_in": 24 * 3600})

            role, rec = ctrl.who(self.bearer())

            # ---- admin ----
            if path.startswith("/api/admin/"):
                if role != "admin":
                    raise ApiError(403, "admin only")
                if method == "GET" and path == "/api/admin/state":
                    return self.send_json(ctrl.admin_state())
                if method == "POST" and path == "/api/admin/logout":
                    ctrl.logout(self.bearer())
                    return self.send_json({"ok": True})
                m = re.fullmatch(r"/api/admin/workers/(w[0-9a-f]{8})/(\w+)", path)
                if method == "POST" and m:
                    ctrl.admin_worker_action(m.group(1), m.group(2), self.body_json())
                    return self.send_json({"ok": True})
                if path == "/api/admin/tokens":
                    if method == "GET":
                        return self.send_json({"tokens": ctrl.store.list_tokens()})
                    if method == "POST":
                        b = self.body_json()
                        kind = b.get("kind")
                        if kind not in ("enroll", "client"):
                            raise ApiError(400, "kind must be enroll or client")
                        tid, secret = ctrl.store.create_token(
                            kind, clean_name(b.get("name") or kind, 40), float(b.get("hours") or 24) if kind == "enroll" else (float(b["hours"]) if b.get("hours") else None),
                            int(b.get("uses") or 1) if kind == "enroll" else None)
                        return self.send_json({"id": tid, "token": secret, "note": "shown once, store it now"})
                m = re.fullmatch(r"/api/admin/tokens/(t[0-9a-f]{8})", path)
                if method == "DELETE" and m:
                    ctrl.store.revoke_token(m.group(1))
                    return self.send_json({"ok": True})
                m = re.fullmatch(r"/api/admin/jobs/(j[0-9a-f]{10})/cancel", path)
                if method == "POST" and m:
                    ctrl.cancel_job(m.group(1))
                    return self.send_json({"ok": True})
                raise ApiError(404, "not found")

            # ---- worker ----
            if path.startswith("/api/worker/"):
                if role != "worker":
                    raise ApiError(403, "worker only")
                if method == "POST" and path == "/api/worker/poll":
                    return self.send_json(ctrl.worker_poll(rec, self.body_json()))
                if method == "POST" and path == "/api/worker/terminated":
                    ctrl.worker_terminated(rec)
                    return self.send_json({"ok": True})
                m = re.fullmatch(r"/api/worker/jobs/(j[0-9a-f]{10})/(input|output|checkpoint|result)", path)
                if m:
                    jid, what = m.groups()
                    job = ctrl.store.get_job(jid)
                    if not job or job["worker_id"] != rec["id"]:
                        raise ApiError(404, "job not assigned to this device")
                    if what == "result" and method == "POST":
                        return self.send_json(ctrl.worker_result(rec, jid, self.body_json()))
                    if what == "input" and method == "GET":
                        return self.send_blob(ctrl.blob_path(jid, "input"))
                    if what == "checkpoint" and method == "GET":
                        return self.send_blob(ctrl.blob_path(jid, "checkpoint"))
                    if what in ("output", "checkpoint") and method == "PUT":
                        self.save_blob(ctrl.blob_path(jid, what))
                        if what == "output":
                            ctrl.store.update("jobs", jid, has_output=1)
                        else:
                            ctrl.store.update("jobs", jid, checkpoint_version=job["checkpoint_version"] + 1)
                        return self.send_json({"ok": True})
                raise ApiError(404, "not found")

            # ---- jobs (client or admin) ----
            if path == "/api/jobs" and method == "POST":
                if role not in ("client", "admin"):
                    raise ApiError(403, "client token required")
                jid = ctrl.submit_job(rec["name"] if rec else "admin", self.body_json())
                return self.send_json({"id": jid, "state": ctrl.store.get_job(jid)["state"]})
            m = re.fullmatch(r"/api/jobs/(j[0-9a-f]{10})(?:/(input|start|output|cancel))?", path)
            if m:
                if role not in ("client", "admin"):
                    raise ApiError(403, "client token required")
                jid, what = m.groups()
                job = ctrl.store.get_job(jid)
                if not job:
                    raise ApiError(404, "no such job")
                if what is None and method == "GET":
                    return self.send_json(ctrl.job_public(job))
                if what == "input" and method == "PUT":
                    if job["state"] != "staged":
                        raise ApiError(409, "job is not waiting for input")
                    self.save_blob(ctrl.blob_path(jid, "input"))
                    return self.send_json({"ok": True})
                if what == "start" and method == "POST":
                    ctrl.start_job(jid)
                    return self.send_json({"ok": True})
                if what == "output" and method == "GET":
                    return self.send_blob(ctrl.blob_path(jid, "output"))
                if what == "cancel" and method == "POST":
                    ctrl.cancel_job(jid)
                    return self.send_json({"ok": True})
            raise ApiError(404, "not found")

    return Handler


def serve(ctrl, host, port, dashboard_path, certfile=None, keyfile=None):
    server = ThreadingHTTPServer((host, port), make_handler(ctrl, dashboard_path))
    server.daemon_threads = True
    if certfile:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certfile, keyfile)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server
