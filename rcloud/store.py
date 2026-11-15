"""SQLite state for the controller (workers, jobs, tokens, events). Thread-safe."""
import json
import os
import sqlite3
import threading
import uuid

from .common import now
from . import security as sec

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tokens(id TEXT PRIMARY KEY, kind TEXT, name TEXT, hash TEXT, created REAL,
  expires REAL, max_uses INTEGER, uses INTEGER DEFAULT 0, revoked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS workers(id TEXT PRIMARY KEY, name TEXT, hostname TEXT, os TEXT, key_hash TEXT,
  created REAL, last_seen REAL, online_since REAL, offline_since REAL, paused INTEGER DEFAULT 0,
  mode TEXT DEFAULT 'balanced', terminate_state TEXT DEFAULT '', revoked INTEGER DEFAULT 0,
  pending_cmds TEXT DEFAULT '[]', caps TEXT DEFAULT '{}', stats TEXT DEFAULT '{}', policy TEXT DEFAULT '{}',
  running TEXT DEFAULT '[]');
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, client TEXT, name TEXT, type TEXT, spec TEXT, state TEXT,
  worker_id TEXT, created REAL, started REAL, finished REAL, attempts INTEGER DEFAULT 0,
  max_attempts INTEGER DEFAULT 3, requirements TEXT, result TEXT, error TEXT, has_input INTEGER DEFAULT 0,
  has_output INTEGER DEFAULT 0, checkpoint_version INTEGER DEFAULT 0, priority INTEGER DEFAULT 0,
  assigned_at REAL, timeout_s INTEGER DEFAULT 3600);
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, kind TEXT, detail TEXT);
"""

JSON_FIELDS = {"caps", "stats", "policy", "running", "pending_cmds", "spec", "requirements", "result"}


def _decode(row):
    if row is None:
        return None
    d = dict(row)
    for k in JSON_FIELDS:
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d


class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.commit()

    # ---- generic ----
    def run(self, sql, args=()):
        with self.lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def one(self, sql, args=()):
        with self.lock:
            return _decode(self.db.execute(sql, args).fetchone())

    def all(self, sql, args=()):
        with self.lock:
            return [_decode(r) for r in self.db.execute(sql, args).fetchall()]

    def update(self, table, row_id, **fields):
        if not fields:
            return
        cols = []
        vals = []
        for k, v in fields.items():
            cols.append(k + "=?")
            vals.append(json.dumps(v) if k in JSON_FIELDS and not isinstance(v, str) else v)
        self.run("UPDATE %s SET %s WHERE id=?" % (table, ",".join(cols)), vals + [row_id])

    # ---- settings / events ----
    def get_setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key, value):
        self.run("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def event(self, kind, detail=""):
        self.run("INSERT INTO events(ts,kind,detail) VALUES(?,?,?)", (now(), kind, detail))
        self.run("DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 500")

    def events(self, limit=30):
        return self.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    # ---- tokens (enroll = register a laptop, client = submit jobs) ----
    def create_token(self, kind, name, ttl_hours=None, max_uses=None):
        secret = sec.new_token("en" if kind == "enroll" else "cl")
        token_id = "t" + uuid.uuid4().hex[:8]
        expires = now() + ttl_hours * 3600 if ttl_hours else None
        self.run("INSERT INTO tokens(id,kind,name,hash,created,expires,max_uses) VALUES(?,?,?,?,?,?,?)",
                 (token_id, kind, name, sec.hash_token(secret), now(), expires, max_uses))
        return token_id, secret

    def find_token(self, kind, secret):
        row = self.one("SELECT * FROM tokens WHERE kind=? AND hash=? AND revoked=0", (kind, sec.hash_token(secret)))
        if not row:
            return None
        if row["expires"] and row["expires"] < now():
            return None
        return row

    def use_enroll_token(self, secret):
        with self.lock:
            row = self.find_token("enroll", secret)
            if not row:
                return None
            if row["max_uses"] is not None and row["uses"] >= row["max_uses"]:
                return None
            self.run("UPDATE tokens SET uses=uses+1 WHERE id=?", (row["id"],))
            return row

    def list_tokens(self):
        return self.all("SELECT id,kind,name,created,expires,max_uses,uses,revoked FROM tokens ORDER BY created DESC")

    def revoke_token(self, token_id):
        self.run("UPDATE tokens SET revoked=1 WHERE id=?", (token_id,))

    # ---- workers ----
    def create_worker(self, name, hostname, os_name, caps, policy, mode="balanced"):
        """Returns (worker_id, secret). The bearer token workers send is worker_id + "." + secret —
        callers build that themselves; this does not return the combined form, to avoid double-prefixing."""
        wid = "w" + uuid.uuid4().hex[:8]
        secret = sec.new_token("wk")
        self.run("INSERT INTO workers(id,name,hostname,os,key_hash,created,mode,caps,policy) VALUES(?,?,?,?,?,?,?,?,?)",
                 (wid, name, hostname, os_name, sec.hash_token(secret), now(), mode, json.dumps(caps), json.dumps(policy)))
        return wid, secret

    def get_worker(self, wid):
        return self.one("SELECT * FROM workers WHERE id=?", (wid,))

    def list_workers(self):
        return self.all("SELECT * FROM workers ORDER BY created")

    def auth_worker(self, bearer):
        if "." not in bearer:
            return None
        wid, secret = bearer.split(".", 1)
        w = self.get_worker(wid)
        if w and sec.token_matches(secret, w["key_hash"]):
            return w
        return None

    def push_command(self, wid, command):
        with self.lock:
            w = self.get_worker(wid)
            if w:
                cmds = w["pending_cmds"] + [command]
                self.update("workers", wid, pending_cmds=cmds)

    def take_commands(self, wid):
        with self.lock:
            w = self.get_worker(wid)
            cmds = w["pending_cmds"] if w else []
            if cmds:
                self.update("workers", wid, pending_cmds=[])
            return cmds

    # ---- jobs ----
    def create_job(self, client, name, jtype, spec, requirements, timeout_s, max_attempts, has_input):
        jid = "j" + uuid.uuid4().hex[:10]
        state = "staged" if has_input else "queued"
        self.run("INSERT INTO jobs(id,client,name,type,spec,state,created,max_attempts,requirements,has_input,timeout_s) "
                 "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (jid, client, name, jtype, json.dumps(spec), state, now(), max_attempts, json.dumps(requirements),
                  1 if has_input else 0, timeout_s))
        return jid

    def get_job(self, jid):
        return self.one("SELECT * FROM jobs WHERE id=?", (jid,))

    def recent_jobs(self, limit=60):
        return self.all("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))

    def job_counts(self):
        return {r["state"]: r["n"] for r in self.all("SELECT state, COUNT(*) AS n FROM jobs GROUP BY state")}

    def claim_job(self, worker, offer):
        """Pull-based scheduling: give the oldest queued job that fits what this worker offers right now."""
        with self.lock:
            for j in self.all("SELECT * FROM jobs WHERE state='queued' ORDER BY priority DESC, created ASC"):
                req = j["requirements"] or {}
                if j["type"] not in offer["allowed_types"]:
                    continue
                if req.get("cores", 1) > offer["free_cores"]:
                    continue
                if (req.get("gpu") or j["type"] == "gpu_python") and not offer["gpu_free"]:
                    continue
                mem = offer.get("free_mem_mb")
                if req.get("memory_mb") and mem is not None and req["memory_mb"] > mem:
                    continue
                if not set(req.get("tags", [])) <= set(offer.get("tags", [])):
                    continue
                pin = req.get("worker")
                if pin and pin not in (worker["id"], worker["name"]):
                    continue
                self.update("jobs", j["id"], state="running", worker_id=worker["id"], started=now(),
                            assigned_at=now(), attempts=j["attempts"] + 1, error=None)
                return self.get_job(j["id"])
        return None

    def requeue_worker_jobs(self, wid, reason, only_ids=None):
        """Jobs that were running on a lost worker go back to the queue, or fail when no retries are left."""
        moved = []
        with self.lock:
            for j in self.all("SELECT * FROM jobs WHERE state='running' AND worker_id=?", (wid,)):
                if only_ids is not None and j["id"] not in only_ids:
                    continue
                if j["attempts"] >= j["max_attempts"]:
                    self.update("jobs", j["id"], state="failed", finished=now(), error=reason + " (no retries left)")
                else:
                    self.update("jobs", j["id"], state="queued", worker_id=None, error=reason + " (retrying)")
                moved.append(j["id"])
        return moved
