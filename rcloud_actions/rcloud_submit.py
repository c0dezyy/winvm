#!/usr/bin/env python3
"""Runs INSIDE a GitHub Actions job. Submits work to your rcloud controller, uploads an input
folder if given, waits for completion, downloads the output, and prints a short summary.
Needs only the standard library, so no 'pip install' step is required in the workflow.

Usage:
  python rcloud_submit.py --server https://your-controller --token $RCLOUD_TOKEN \
      --type python --entry main.py --input ./payload --output ./result --timeout 1800
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from rcloud.client import ApiClient  # noqa: E402
from rcloud.jobs import zip_path      # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", required=True)
    ap.add_argument("--token", required=True, help="an rcloud client token (RCLOUD_TOKEN secret)")
    ap.add_argument("--type", default="python", choices=("python", "gpu_python", "cpu_benchmark", "gpu_info"))
    ap.add_argument("--entry", default="main.py")
    ap.add_argument("--args", nargs="*", default=[])
    ap.add_argument("--input", help="folder to zip and send as the job input")
    ap.add_argument("--output", default="rcloud_output", help="folder to extract the job output into")
    ap.add_argument("--cores", type=int, default=1)
    ap.add_argument("--memory-mb", type=int)
    ap.add_argument("--gpu", action="store_true")
    ap.add_argument("--worker", help="pin to one device by name or id")
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--poll-every", type=float, default=5.0)
    ap.add_argument("--no-verify-ssl", action="store_true")
    a = ap.parse_args()

    c = ApiClient(a.server, a.token, verify_ssl=not a.no_verify_ssl)
    requirements = {"cores": a.cores}
    if a.memory_mb:
        requirements["memory_mb"] = a.memory_mb
    if a.gpu:
        requirements["gpu"] = True
    if a.worker:
        requirements["worker"] = a.worker

    body = {"type": a.type, "name": "github-actions", "timeout_s": a.timeout, "requirements": requirements}
    if a.type in ("python", "gpu_python"):
        body["entry"], body["args"] = a.entry, a.args
    has_input = bool(a.input)
    body["input"] = has_input

    status, resp = c.post("/api/jobs", body)
    if status != 200:
        sys.exit("Could not create the job: %s" % resp)
    jid = resp["id"]
    print("Job", jid, "created.")

    if has_input:
        zpath = "/tmp/rcloud_input.zip" if os.name != "nt" else os.path.join(os.environ.get("TEMP", "."), "rcloud_input.zip")
        n = zip_path(a.input, zpath)
        print("Zipped %d file(s) from %s" % (n, a.input))
        status, resp = c.request("PUT", "/api/jobs/%s/input" % jid, raw_body=open(zpath, "rb").read(),
                                 headers={"Content-Type": "application/zip"}, timeout=120)
        if status != 200:
            sys.exit("Could not upload the input: %s" % resp)
        status, resp = c.post("/api/jobs/%s/start" % jid)
        if status != 200:
            sys.exit("Could not start the job: %s" % resp)

    print("Waiting for a device to run it (this VM is temporary, the controller is not)...")
    deadline = time.time() + a.timeout + 60
    last_state = None
    while time.time() < deadline:
        status, job = c.get("/api/jobs/%s" % jid)
        if status != 200:
            sys.exit("Could not check the job: %s" % job)
        if job["state"] != last_state:
            print("  state:", job["state"], "on", job.get("worker_id") or "-")
            last_state = job["state"]
        if job["state"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(a.poll_every)
    else:
        sys.exit("Timed out waiting for the job.")

    print(json.dumps(job.get("result") or {}, indent=2))
    if job.get("has_output"):
        from rcloud.jobs import safe_extract
        zpath = "rcloud_output.zip"
        c.get_file("/api/jobs/%s/output" % jid, zpath, timeout=120)
        safe_extract(zpath, a.output)
        print("Output extracted to", a.output)

    if job["state"] != "succeeded":
        sys.exit("Job %s: %s" % (job["state"], job.get("error")))
    print("Job succeeded.")


if __name__ == "__main__":
    main()
