"""Build the downloadable AI Interconnection Center zip.

Run:  python build_package.py
Out:  dist/AI-Interconnection-Center-<version>.zip   (served by the hub at GET /download)

The package holds the app only: no channel logs, tasks or keys. A fresh
install creates an empty "Main" channel on first start.
"""

import os
import py_compile
import sys
import zipfile

ROOT = os.path.abspath(os.path.dirname(__file__))
sys.path.insert(0, ROOT)
import channel_viewer as cv  # noqa: E402

FILES = [
    "channel_viewer.py", "aic_store.py", "aic_llm.py", "aic_mcp.py", "channel_cli.py",
    "append_hub_entry.py", "claim_guard.py", "launch_viewer.py", "start.bat", "README.md",
    "web/index.html", "web/app.js", "web/app.css", "web/icon.svg",
]


def main():
    ver = cv.APP_VERSION
    missing = [f for f in FILES if not os.path.exists(os.path.join(ROOT, f))]
    if missing:
        print("MISSING: %s" % missing)
        return 1
    for f in FILES:
        if f.endswith(".py"):
            py_compile.compile(os.path.join(ROOT, f), doraise=True)
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    zpath = os.path.join(ROOT, "dist", "AI-Interconnection-Center-%s.zip" % ver)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in FILES:
            z.write(os.path.join(ROOT, f), "ai-interconnection-center/" + f)
        z.writestr("ai-interconnection-center/version.txt", "AI Interconnection Center %s\n" % ver)
    print("version : %s" % ver)
    print("zip     : %s (%d bytes, %d files)" % (zpath, os.path.getsize(zpath), len(FILES) + 1))
    print("all Python files compile OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
