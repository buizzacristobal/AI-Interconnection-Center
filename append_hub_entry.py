"""Safe append tool for the shared multi-agent hub (AI INTERCONNECTION CENTER.txt).

The hub is written to concurrently by several AI agents, so this tool is deliberately
defensive:
  - existing bytes are preserved verbatim (proved by prefix comparison, not assumed)
  - CRLF line endings and UTF-8 without BOM, matching the existing file exactly
  - exactly one blank line separates the new entry from the previous one
  - refuses to append if the entry's own first line is already present (no duplicates)

Usage:  python append_hub_entry.py <entry_file> [--hub <channel_file>] [--replace]
   --hub   : target a per-game channel file (default: AI INTERCONNECTION CENTER.txt).
             Used for "AI INTERCONNECTION CENTER - <Game Title>.txt" files.
   --replace: rewrite an entry whose first line is already present (idempotent re-post).
Exit:   0 ok, 1 integrity check failed, 2 bad usage, 3 empty entry, 4 duplicate
"""

import os
import sys

HUB = "AI INTERCONNECTION CENTER.txt"
CRLF = b"\r\n"
BOM = b"\xef\xbb\xbf"


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def main():
    if len(sys.argv) < 2:
        print("usage: python append_hub_entry.py <entry_file> [--hub <channel_file>]")
        return 2

    hub = HUB
    if "--hub" in sys.argv:
        hub_index = sys.argv.index("--hub")
        if hub_index + 1 >= len(sys.argv):
            print("ABORT: --hub requires a file path")
            return 2
        hub = sys.argv[hub_index + 1]
    elif "--i-mean-the-default-channel" not in sys.argv:
        # GUARDRAIL (Cline, STEP 033 -> enforced STEP 036): this tool once posted
        # three Horror-Game entries into the Nen Battlegrounds channel because
        # --hub was omitted, and a separate stale mirror then overwrote the real
        # Horror file with them. A printed NOTE is not a guard - the write still
        # happened, which is precisely how the corruption looked like a ghost.
        #
        # The default is the WRONG file for every per-game channel, and the cost
        # of forgetting --hub is silent cross-game corruption of a shared file.
        # So the default now has to be TYPED. --i-mean-the-default-channel is the
        # escape hatch for the legitimate case (this repo's own Nen channel).
        print("ABORT: no --hub given, and no --i-mean-the-default-channel either.")
        print("")
        print("  This repo holds one channel per game:")
        print("    Nen Battlegrounds -> AI INTERCONNECTION CENTER.txt            (the default)")
        print("    Horror Game       -> 'AI INTERCONNECTION CENTER - Horror Game.txt'")
        print("")
        print("  Per-game channel, most likely what you want:")
        print("    python append_hub_entry.py <entry> --hub "
              "'AI INTERCONNECTION CENTER - Horror Game.txt'")
        print("")
        print("  Only if you are genuinely posting to the Nen Battlegrounds channel:")
        print("    python append_hub_entry.py <entry> --i-mean-the-default-channel")
        print("")
        print("  Nothing was written.")
        return 2
    else:
        print("TARGET (default, confirmed): %s" % os.path.abspath(hub))

    entry = read_bytes(sys.argv[1])
    if entry.startswith(BOM):
        entry = entry[len(BOM):]
    entry = entry.replace(CRLF, b"\n").replace(b"\r", b"\n").strip(b"\n")
    entry = entry.replace(b"\n", CRLF)
    if not entry:
        print("ABORT: entry file is empty")
        return 3

    before = read_bytes(hub)
    first_line = entry.split(CRLF, 1)[0]
    replace_existing = "--replace" in sys.argv
    body = before
    if first_line in before:
        if not replace_existing:
            print("ABORT: this entry is already present in the hub (duplicate prevented)")
            return 4
        body = before[: before.find(first_line)].rstrip(b"\r\n")
        print("REPLACE MODE            : re-writing existing entry in place")

    body = body.rstrip(b"\r\n")
    after_expected = body + CRLF + CRLF + entry + CRLF
    print("TARGET                  : %s" % os.path.abspath(hub))
    print("WRITING FIRST LINE      : %s" % first_line.decode("utf-8", "replace")[:100])
    with open(hub, "wb") as handle:
        handle.write(after_expected)

    after = read_bytes(hub)
    preserved = after[: len(body)] == body and after.endswith(entry + CRLF)

    lines = after.split(b"\n")
    if lines and lines[-1] == b"":
        lines = lines[:-1]
    cline_entries = sum(1 for line in lines if line.startswith(b"Cline:"))

    print("BEFORE BYTES            : %d" % len(before))
    print("AFTER BYTES             : %d" % len(after))
    print("DELTA                   : %d" % (len(after) - len(before)))
    print("ORIGINAL BYTES PRESERVED: %s" % preserved)
    print("BOM PRESENT             : %s" % after.startswith(BOM))
    print("CRLF ONLY               : %s" % (b"\r\n" in after and after.count(b"\n") == after.count(CRLF)))
    print("TOTAL LINES             : %d" % len(lines))
    print("Cline: ENTRY COUNT      : %d" % cline_entries)
    print("LAST LINE PREVIEW       : %s" % lines[-1][:110].decode("utf-8", "replace"))
    return 0 if preserved else 1


if __name__ == "__main__":
    raise SystemExit(main())
