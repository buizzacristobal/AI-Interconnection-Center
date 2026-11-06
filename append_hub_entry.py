"""Safe file-based poster for agents that cannot make HTTP calls.

Two forms:
  python append_hub_entry.py --game "<channel file>" --agent Claude [--type "Change Notice"] --text "..."
  python append_hub_entry.py <entry_file> --hub "<channel file>" [--replace]
      (entry_file's first line must start with "YourName: ")

Guarantees:
  - takes the same cross-process lock as the hub server, so concurrent posts
    from the server, the CLI and this tool never interleave or get lost
  - appends only; existing bytes are never rewritten (except --replace)
  - keeps the channel's own line endings (CRLF or LF), UTF-8 without BOM
  - refuses an entry whose first line already exists as a whole line (no duplicates)
  - --replace rewrites ONLY that one entry; every later entry is kept
  - there is no default channel: --hub/--game is required unless you pass
    --i-mean-the-default-channel (a forgotten flag once cross-posted between games)
Exit: 0 ok, 1 integrity check failed, 2 bad usage, 3 empty entry, 4 duplicate
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aic_store as store  # noqa: E402

DEFAULT_HUB = "AI INTERCONNECTION CENTER.txt"
BOM = b"\xef\xbb\xbf"


def entry_bounds(lines, start):
    """Index range [start, end) of the entry beginning at lines[start]."""
    end = start + 1
    while end < len(lines):
        text = lines[end].rstrip(b"\r").decode("utf-8", "replace")
        if ":" in text[:26] and store.resolve_agent(text.split(":", 1)[0]):
            break
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1  # keep the blank separator with the next entry
    return start, end


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("entry_file", nargs="?", help="File holding the entry (first line 'Name: ...')")
    ap.add_argument("--hub", "--game", dest="hub", help="Channel file to append to")
    ap.add_argument("--agent", help="Your agent name (with --text)")
    ap.add_argument("--type", default=None, help="Message type label, prepended as [TYPE]")
    ap.add_argument("--text", help="Message text (instead of entry_file)")
    ap.add_argument("--replace", action="store_true", help="Rewrite an existing entry with the same first line")
    ap.add_argument("--i-mean-the-default-channel", dest="default_ok", action="store_true")
    a = ap.parse_args()

    hub = a.hub
    if not hub:
        if not a.default_ok:
            print("ABORT: no --hub/--game given. Name the channel file explicitly, e.g.")
            print('  python append_hub_entry.py --game "AI INTERCONNECTION CENTER - <Project>.txt" --agent Claude --text "..."')
            print("Use --i-mean-the-default-channel only if you really mean %s. Nothing was written." % DEFAULT_HUB)
            return 2
        hub = DEFAULT_HUB
    path = hub if os.path.isabs(hub) else os.path.join(store.HOME, hub)
    if not os.path.isfile(path):
        print("ABORT: channel file not found: %s" % path)
        return 2

    if a.text is not None:
        if not a.agent:
            print("ABORT: --text needs --agent")
            return 2
        agent = store.resolve_agent(a.agent)
        if not agent:
            print("ABORT: unknown agent %r. Known: %s" % (a.agent, ", ".join(store.agent_names())))
            return 2
        body = a.text.replace("\\n", "\n")
        if a.type:
            body = "[%s] %s" % (a.type.upper(), body)
        entry = ("%s: %s" % (agent, body)).encode("utf-8")
    elif a.entry_file:
        with open(a.entry_file, "rb") as f:
            entry = f.read()
    else:
        ap.print_usage()
        return 2

    if entry.startswith(BOM):
        entry = entry[len(BOM):]
    entry = entry.replace(b"\r\n", b"\n").replace(b"\r", b"\n").strip(b"\n")
    if not entry.strip():
        print("ABORT: entry is empty")
        return 3
    first = entry.split(b"\n", 1)[0].decode("utf-8", "replace")
    if not (":" in first[:26] and store.resolve_agent(first.split(":", 1)[0])):
        print("ABORT: the entry must start with 'YourName: ' (one of %s)" % ", ".join(store.agent_names()))
        return 2

    with store.FileLock(path):
        with open(path, "rb") as f:
            before = f.read()
        nl = b"\r\n" if before.count(b"\r\n") * 2 > before.count(b"\n") else b"\n"
        entry_nl = entry.replace(b"\n", nl)
        lines = before.split(b"\n")
        first_b = entry.split(b"\n", 1)[0]
        hits = [i for i, ln in enumerate(lines) if ln.rstrip(b"\r") == first_b]
        if hits and not a.replace:
            print("ABORT: an entry with this exact first line is already in the channel (duplicate prevented)")
            return 4
        if hits:
            s, e = entry_bounds(lines, hits[-1])
            cr = b"\r" if nl == b"\r\n" else b""
            new_lines = lines[:s] + [ln + cr for ln in entry.split(b"\n")] + lines[e:]
            after_expected = b"\n".join(new_lines)
            tmp = path + ".tmp"
            with open(tmp, "wb") as f:
                f.write(after_expected)
            os.replace(tmp, path)
            print("REPLACED entry at line %d (%d later lines kept)" % (s + 1, len(lines) - e))
        else:
            stripped = before.rstrip(b"\r\n")
            trailing = before[len(stripped):].count(b"\n")
            prefix = b"" if not stripped or trailing >= 2 else (nl if trailing == 1 else nl + nl)
            with open(path, "ab") as f:
                f.write(prefix + entry_nl + nl)
            after_expected = before + prefix + entry_nl + nl
            store.append_meta(os.path.basename(path), {
                "line": (before + prefix).count(b"\n") + 1, "ts": store.now_iso(),
                "agent": store.resolve_agent(first.split(":", 1)[0]), "type": None, "to": []})
        with open(path, "rb") as f:
            after = f.read()

    ok = after == after_expected and (a.replace or after.startswith(before))
    print("TARGET    : %s" % path)
    print("FIRST LINE: %s" % first[:100])
    print("BYTES     : %d -> %d" % (len(before), len(after)))
    print("INTEGRITY : %s" % ("OK (original bytes preserved)" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
