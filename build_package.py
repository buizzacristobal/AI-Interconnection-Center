"""Build the downloadable AI Interconnection Center zip.

Run:  python build_package.py
Out:  dist/AI-Interconnection-Center-<version>.zip  (+ staged folder dist/app/)

The viewer serves the zip itself at GET /download.
"""

import os
import shutil
import sys
import zipfile

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import channel_viewer as cv

ROOT = os.path.abspath(os.path.dirname(__file__))
FILES = [
    "channel_viewer.py",
    "channel_cli.py",
    "launch_viewer.py",
    "append_hub_entry.py",
    "dist/README.txt",
    "dist/start.bat",
    "AI INTERCONNECTION CENTER.txt",
    "AI INTERCONNECTION CENTER.txt.tree.json",
    "AI INTERCONNECTION CENTER - Horror Game.txt",
    "AI INTERCONNECTION CENTER - Horror Game.txt.tree.json",
]


def main():
    ver = cv.APP_VERSION
    stage = os.path.join(ROOT, "dist", "app")
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(stage)
    missing = []
    for rel in FILES:
        src = os.path.join(ROOT, rel)
        if not os.path.exists(src):
            missing.append(rel)
            continue
        shutil.copy2(src, os.path.join(stage, os.path.basename(rel)))
    with open(os.path.join(stage, "version.txt"), "w", encoding="utf-8") as f:
        f.write("AI Interconnection Center %s\n" % ver)
    zipname = "AI-Interconnection-Center-%s.zip" % ver
    zpath = os.path.join(ROOT, "dist", zipname)
    if os.path.exists(zpath):
        os.remove(zpath)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(os.listdir(stage)):
            z.write(os.path.join(stage, name), "ai-interconnection-center/" + name)
    size = os.path.getsize(zpath)
    print("version : %s" % ver)
    print("zip     : %s (%d bytes)" % (zpath, size))
    print("staged  : %s" % sorted(os.listdir(stage)))
    if missing:
        print("MISSING : %s" % missing)
        return 1
    # sanity: the staged viewer must compile
    import py_compile
    py_compile.compile(os.path.join(stage, "channel_viewer.py"), doraise=True)
    print("staged viewer compiles OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
