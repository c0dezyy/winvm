"""Controller CLI: rcloud-controller serve / create-token / init-admin."""
import argparse
import os
import sys

from .common import data_dir
from .controller import Controller, serve

DASHBOARD = os.path.join(os.path.dirname(os.path.dirname(__file__)), "dashboard", "index.html")


def cmd_serve(a):
    ctrl = Controller(a.data_dir or data_dir("controller"), offline_after=a.offline_after,
                      cors_origins=tuple(a.cors) if a.cors else ())
    pw = ctrl.init_admin(a.admin_password)
    if pw:
        print("First run: admin password is below. It will not be shown again.")
        print("  ADMIN PASSWORD:", pw)
    ctrl.start()
    dashboard = a.dashboard or DASHBOARD
    httpd = serve(ctrl, a.host, a.port, dashboard, a.cert, a.key)
    scheme = "https" if a.cert else "http"
    print("rcloud controller listening on %s://%s:%s  (dashboard at /)" % (scheme, a.host, a.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.stop()
        httpd.shutdown()


def cmd_token(a):
    ctrl = Controller(a.data_dir or data_dir("controller"))
    tid, secret = ctrl.store.create_token(a.kind, a.name, a.hours, a.uses if a.kind == "enroll" else None)
    print("%s token %s created. Shown once:" % (a.kind, tid))
    print(" ", secret)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rcloud-controller")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the controller")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8443)
    s.add_argument("--data-dir")
    s.add_argument("--dashboard")
    s.add_argument("--admin-password", help="set this password on first run instead of a random one")
    s.add_argument("--offline-after", type=float, default=30.0, help="seconds without a poll before a worker is marked offline")
    s.add_argument("--cert", help="TLS certificate (PEM) - leave unset for plain HTTP behind your own reverse proxy / VPN")
    s.add_argument("--key", help="TLS private key (PEM)")
    s.add_argument("--cors", nargs="*", help="origins allowed to call the API from a browser, or * for any")
    s.set_defaults(func=cmd_serve)

    t = sub.add_parser("create-token", help="create an enroll or client token")
    t.add_argument("kind", choices=("enroll", "client"))
    t.add_argument("name")
    t.add_argument("--hours", type=float, default=24)
    t.add_argument("--uses", type=int, default=1, help="enroll tokens only: how many devices may use it")
    t.add_argument("--data-dir")
    t.set_defaults(func=cmd_token)

    a = ap.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
