"""Git pre-commit guard: refuse commits that touch files another agent has claimed.

Claims are advisory in the hub; this turns them into a hard stop at commit time.

  python claim_guard.py --install            install as .git/hooks/pre-commit (in the current repo)
  python claim_guard.py                      check the staged files now

Env: AIC_AGENT (who is committing; required), AIC_GAME (project channel id),
     AIC_URL (default http://127.0.0.1:19843), AIC_KEY (only if access keys are on),
     AIC_GUARD_STRICT=1 to block when the hub is unreachable (default: warn and allow).
Claim paths are matched against repo-relative paths (folders and globs included).
"""

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from aic_store import paths_overlap  # noqa: E402

URL = (os.environ.get("AIC_URL") or "http://127.0.0.1:%s" % os.environ.get("AIC_PORT", "19843")).rstrip("/")


def staged_files():
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "-z"], capture_output=True, check=True)
    return [p for p in out.stdout.decode("utf-8", "replace").split("\0") if p]


def install():
    git_dir = subprocess.run(["git", "rev-parse", "--git-dir"], capture_output=True, text=True, check=True).stdout.strip()
    hook = os.path.join(git_dir, "hooks", "pre-commit")
    if os.path.exists(hook) and "claim_guard" not in open(hook, encoding="utf-8", errors="replace").read():
        print("A different pre-commit hook already exists at %s. Add this line to it instead:" % hook)
        print('  "%s" "%s"' % (sys.executable, os.path.abspath(__file__)))
        return 1
    with open(hook, "w", encoding="utf-8", newline="\n") as f:
        f.write('#!/bin/sh\nexec "%s" "%s"\n' % (sys.executable.replace("\\", "/"), os.path.abspath(__file__).replace("\\", "/")))
    os.chmod(hook, 0o755)
    print("Installed %s. Set AIC_AGENT (and AIC_GAME) in the agent's environment." % hook)
    return 0


def main():
    if "--install" in sys.argv:
        return install()
    agent = os.environ.get("AIC_AGENT", "")
    if not agent:
        print("claim_guard: AIC_AGENT is not set; skipping the claim check.")
        return 0
    q = {"game": os.environ["AIC_GAME"]} if os.environ.get("AIC_GAME") else {}
    req = urllib.request.Request(URL + "/claims?" + urllib.parse.urlencode(q),
                                 headers={"X-AIC-Key": os.environ.get("AIC_KEY", "")})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            claims = json.loads(r.read().decode("utf-8"))["claims"]
    except (urllib.error.URLError, OSError, ValueError, KeyError) as e:
        print("claim_guard: could not reach the hub at %s (%s)." % (URL, e))
        return 1 if os.environ.get("AIC_GUARD_STRICT") == "1" else 0
    blocked = []
    for path in staged_files():
        for c in claims:
            if c["agent"].lower() != agent.lower() and any(paths_overlap(path, p) for p in c["paths"]):
                blocked.append((path, c))
    if not blocked:
        return 0
    print("claim_guard: commit blocked - these files are claimed by other agents:")
    for path, c in blocked:
        print("  %s  <- %s %s%s (until %s)" % (path, c["agent"], c["id"], (' "%s"' % c["note"]) if c["note"] else "", c["expires_at"]))
    print("Coordinate in the channel (@mention them), or unstage those files. The human can release claims in the dashboard.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
