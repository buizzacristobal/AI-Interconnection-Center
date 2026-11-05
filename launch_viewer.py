"""Launcher for AI Interconnection Center.

Starts channel_viewer.py in the background (unless the hub is already up)
and opens the dashboard. Safe to run with pythonw.exe (no console window).
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

PORT = int(os.environ.get("AIC_PORT", "19843"))
URL = "http://127.0.0.1:%d" % PORT
ROOT = os.path.abspath(os.path.dirname(__file__))
SCRIPT_PATH = os.path.join(ROOT, "channel_viewer.py")


def hub_status():
    """'aic' if our hub answers, 'other' if something else holds the port, None if free."""
    try:
        with urllib.request.urlopen(URL + "/health", timeout=0.8) as r:
            return "aic" if json.loads(r.read().decode("utf-8")).get("ok") else "other"
    except (OSError, ValueError):
        try:
            import socket
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                return "other"
        except OSError:
            return None


def main():
    status = hub_status()
    if status == "other":
        print("Port %d is used by another program. Set AIC_PORT to a free port and try again." % PORT)
        return 1
    if status is None:
        runner = sys.executable
        pythonw = os.path.join(os.path.dirname(runner), "pythonw.exe")
        if os.path.exists(pythonw):
            runner = pythonw  # no console window on Windows
        flags = 0x08000000 | 0x00000008 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW | DETACHED_PROCESS
        subprocess.Popen([runner, SCRIPT_PATH], cwd=ROOT, creationflags=flags, close_fds=True,
                         start_new_session=sys.platform != "win32")
        for _ in range(40):
            time.sleep(0.2)
            if hub_status() == "aic":
                break
        else:
            print("The hub did not start. Run python channel_viewer.py to see the error.")
            return 1
    webbrowser.open(URL)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
