"""Entry point for the worker program: a small background app with a visible tray icon
(so it is never hidden from the person using the laptop) plus a --enroll / --headless /
--uninstall CLI for setup and for running on servers with no desktop.
"""
import argparse
import sys
import threading

from . import startup, uninstall
from .worker import Worker

TRAY_TOOLTIP = "RCloud Worker"


def _icon_image(color):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.ellipse((18, 18, 46, 46), fill=(255, 255, 255, 255))
    return img


def run_tray(worker):
    try:
        import pystray
    except Exception:
        print("Tray icon needs the optional 'pystray' and 'Pillow' packages; running headless instead.")
        return run_headless(worker)

    state = {"text": "Starting...", "color": (120, 120, 120)}

    def on_status(state_text="", detail=""):
        colors = {"connected": (46, 204, 143), "error": (255, 92, 122), "not enrolled": (255, 176, 32),
                  "terminating": (255, 92, 122), "removed by controller": (255, 92, 122)}
        state["text"] = state_text + (" - " + detail if detail else "")
        state["color"] = colors.get(state_text, (120, 120, 120))
        icon.icon = _icon_image(state["color"])
        icon.title = TRAY_TOOLTIP + "\n" + state["text"]

    worker.on_status = on_status

    def item_status(item):
        return state["text"]

    def pause(icon_, item):
        worker.set_mode("gaming")

    def balanced(icon_, item):
        worker.set_mode("balanced")

    def performance(icon_, item):
        worker.set_mode("performance")

    def quit_app(icon_, item):
        worker.stop()
        icon_.stop()

    def do_uninstall(icon_, item):
        worker.stop()
        icon_.stop()
        uninstall.self_destruct(worker.config_dir)

    menu = pystray.Menu(
        pystray.MenuItem(item_status, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Pause sharing (gaming mode)", pause, radio=True, checked=lambda i: worker.mode() == "gaming"),
        pystray.MenuItem("Balanced", balanced, radio=True, checked=lambda i: worker.mode() == "balanced"),
        pystray.MenuItem("Performance", performance, radio=True, checked=lambda i: worker.mode() == "performance"),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Remove RCloud from this device", do_uninstall),
        pystray.MenuItem("Quit (stop sharing until next login)", quit_app),
    )
    icon = pystray.Icon("rcloud", _icon_image(state["color"]), TRAY_TOOLTIP, menu)
    t = threading.Thread(target=worker.run_forever, daemon=True)
    t.start()
    icon.run()
    worker.stop()


def run_headless(worker):
    def on_status(state_text="", detail=""):
        print("[rcloud]", state_text, detail)
    worker.on_status = on_status
    try:
        worker.run_forever()
    except KeyboardInterrupt:
        worker.stop()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="rcloud-worker")
    ap.add_argument("--enroll", nargs=2, metavar=("SERVER_URL", "TOKEN"), help="register this device once")
    ap.add_argument("--name", help="name to show on the dashboard (with --enroll)")
    ap.add_argument("--no-verify-ssl", action="store_true", help="accept a self-signed controller certificate")
    ap.add_argument("--tray", action="store_true", help="run with a tray icon (default on a desktop)")
    ap.add_argument("--headless", action="store_true", help="run with no tray icon (servers, SSH)")
    ap.add_argument("--install-startup", action="store_true", help="start automatically at login")
    ap.add_argument("--uninstall", action="store_true", help="remove the startup entry, config and this program")
    ap.add_argument("--config-dir", help="use a different config folder (for tests, or multiple instances)")
    a = ap.parse_args(argv)

    if a.uninstall:
        uninstall.self_destruct(Worker(config_dir=a.config_dir).config_dir)
        return

    worker = Worker(config_dir=a.config_dir)

    if a.enroll:
        server, token = a.enroll
        name = worker.enroll(server, token, a.name, verify_ssl=not a.no_verify_ssl)
        print("Enrolled as %r on %s" % (name, server))
        if a.install_startup:
            path = startup.install()
            print("Will start automatically at login:", path)
        if not (a.tray or a.headless):
            return

    if a.install_startup and not a.enroll:
        path = startup.install()
        print("Will start automatically at login:", path)

    if not worker.enrolled():
        print("Not enrolled yet. Run once with:  rcloud-worker --enroll SERVER_URL TOKEN")
        sys.exit(1)

    if a.headless:
        run_headless(worker)
    else:
        run_tray(worker)


if __name__ == "__main__":
    main()
