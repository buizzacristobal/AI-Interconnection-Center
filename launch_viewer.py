"""Launcher for AI Interconnection Center.

Ensures channel_viewer.py is running on port 19843 and opens the browser.
Can run silently with pythonw.exe.
"""

import os
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

PORT = int(os.environ.get("AIC_PORT", "19843"))
URL = f"http://127.0.0.1:{PORT}"
ROOT = os.path.abspath(os.path.dirname(__file__))
SCRIPT_PATH = os.path.join(ROOT, "channel_viewer.py")


def is_running():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.8):
            return True
    except OSError:
        return False


def main():
    if not is_running():
        python_exe = sys.executable
        # Try running with pythonw so there's no console window pop-up
        dirname = os.path.dirname(python_exe)
        pythonw = os.path.join(dirname, "pythonw.exe")
        runner = pythonw if os.path.exists(pythonw) else python_exe

        creationflags = 0
        if sys.platform == "win32":
            # DETACHED_PROCESS = 0x00000008, CREATE_NO_WINDOW = 0x08000000
            creationflags = 0x08000000 | 0x00000008

        subprocess.Popen(
            [runner, SCRIPT_PATH],
            cwd=ROOT,
            creationflags=creationflags,
            close_fds=True,
        )

        for _ in range(25):
            time.sleep(0.2)
            if is_running():
                break

    webbrowser.open(URL)


if __name__ == "__main__":
    main()
