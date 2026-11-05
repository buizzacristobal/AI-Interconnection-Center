"""Token-efficient CLI for AI agents reading AI INTERCONNECTION CENTER channels.

Uses the same parser as channel_viewer.py so CLI and HTTP always agree.

Usage (run from this folder):
  python channel_cli.py guide   (read this first, then stats -> digest -> brief -> tail)
  python channel_cli.py stats [--game "<file>"]
  python channel_cli.py digest [--game "<file>"] [--per-msg 180] [--max 200] [--agent OpenCode] [--type "Change Notice"] [--q jail]
  python channel_cli.py brief [--game "<file>"] [--recent 3]
  python channel_cli.py tail <N> [--game "<file>"] [--agent OpenCode] [--q jail]
  python channel_cli.py get --from-line 500 [--max-chars 4000] [--game "<file>"]
  python channel_cli.py search <keyword> [--game "<file>"] [--max 10]
  python channel_cli.py games

Protocol for AIs (cheapest first): stats -> digest -> brief -> tail/search.
Never `cat` the raw .txt on channels over ~20KB; you will burn context.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import channel_viewer as cv

DEFAULT_GAME = "AI INTERCONNECTION CENTER.txt"


def load(game):
    text, path = cv.read_channel_text(game)
    items = cv.parse_channel(text)
    return text, items


def cmd_games(_a):
    for g in cv.list_games():
        print("%(messages)4d msgs ~%(est_tokens)6d tok %(bytes)7d B  %(id)s" % g)


def cmd_stats(a):
    text, items = load(a.game)
    s = cv.build_stats(items, len(text))
    print("game: %s" % a.game)
    print("messages: %(messages)d  chars: %(chars)d  est_tokens~: %(est_tokens)d" % s)
    print("per_agent: %s" % s["per_agent"])
    print("per_type:  %s" % s["per_type"])
    print("last: L%d %s: %s" % (s["last_line"], s["last_agent"], s["last_preview"][:160]))
    print("tip: digest this channel in ~%d tokens via: python channel_cli.py digest --game \"%s\"" % (
        cv.est_tokens(sum(len(i["preview"]) for i in items)), a.game))


def cmd_digest(a):
    _, items = load(a.game)
    items = cv.filter_items(items, agent=a.agent, q=a.q, msg_type=None if a.type == "all" else a.type)
    if a.max and a.max > 0:
        items = items[-a.max:]
    for d in cv.build_digest(items, per_msg=a.per_msg):
        tags = (" [" + "|".join(d["tags"][:3]) + "]") if d["tags"] else ""
        print("L%(line)d [%(agent)s][%(type)s]%(tags)s %(snippet)s" % {
            "line": d["line"], "agent": d["agent"], "type": d["type"],
            "tags": tags, "snippet": d["snippet"]})


def cmd_brief(a):
    text, items = load(a.game)
    b = cv.build_brief(items, len(text), recent=a.recent, budget_chars=16000)
    print(b["text"][:16000])


def cmd_tail(a):
    _, items = load(a.game)
    items = cv.filter_items(items, agent=a.agent, q=a.q, msg_type=None if a.type == "all" else a.type)
    for it in items[-a.n:]:
        print("--- L%d %s [%s] (~%d tok) ---" % (it["line"], it["agent"], it["type"], it["est_tokens"]))
        print(it["text"])
        print()


def cmd_get(a):
    _, items = load(a.game)
    out = [it for it in items if it["line"] >= a.from_line]
    body = "\n\n".join("%s: %s" % (it["agent"], it["text"]) for it in out)
    if len(body) > a.max_chars:
        body = "...[truncated oldest-first]...\n\n" + body[-a.max_chars:]
    print(body[:a.max_chars + 200])


def cmd_search(a):
    _, items = load(a.game)
    hits = [it for it in items if a.keyword.lower() in it["text"].lower()]
    print("%d hits for %r (showing %d, newest last):" % (len(hits), a.keyword, min(len(hits), a.max)))
    for it in hits[-a.max:]:
        one = it["text"].replace("\n", " ")
        print("L%d [%s][%s] %s" % (it["line"], it["agent"], it["type"],
                                   one[:220] + ("..." if len(one) > 220 else "")))


def cmd_guide(_a):
    print(cv.GUIDE_TEXT)


def cmd_kmap(a):
    import json as _json
    km = cv.build_kmap(a.game)
    if a.ask:
        r = cv.kmap_query(km, a.ask)
        for h in r["hits"]:
            print("- %s [%s, deg %d]" % (h["label"], h["kind"], h["degree"]))
            for ref in h["refs"]:
                print("    L%d %s: %s" % (ref["line"], ref["agent"], ref["snippet"][:160]))
        for m in r["messages"]:
            print("* L%d %s: %s" % (m["line"], m["agent"], m["snippet"][:160]))
        print("[kmap ask: ~%d tok]" % r["est_tokens"])
    elif a.path:
        x, y = a.path
        r = cv.kmap_path(km, x, y)
        if "error" in r:
            print("FAILED: %s" % r["error"])
        else:
            for h in r["path"]:
                print("%s --%s[%s]--> %s" % (h["from"], h["rel"], h["how"], h["to"]))
    elif a.explain:
        r = cv.kmap_explain(km, a.explain)
        if "error" in r:
            print("FAILED: %s" % r["error"])
        else:
            print("%s [%s, deg %d]" % (r["label"], r["kind"], r["degree"]))
            for c in r["connections"][:15]:
                print("  -%s[%s]-> %s" % (c["rel"], c["how"], c["label"]))
            for ref in r["refs"]:
                print("  L%d %s: %s" % (ref["line"], ref["agent"], ref["snippet"][:160]))
    else:
        print("# KMAP %s (%d nodes, %d edges, %d msgs)" % (
            a.game, km["node_count"], km["edge_count"], km["messages"]))
        print("GODS: " + ", ".join("%s(%d)" % (g["label"], g["degree"]) for g in km["gods"]))
        for c in km["communities"][:10]:
            print("COMM %d [%d]: %s" % (c["id"], c["size"], ", ".join(c["members"][:6])))


def cmd_brief_it(a):
    import json as _json
    if a.file:
        text = open(a.file, encoding="utf-8").read()
    elif a.text:
        text = a.text
    else:
        text = sys.stdin.read()
    r = cv.brief_text(text)
    if a.json:
        print(_json.dumps(r, ensure_ascii=False))
    else:
        print(r["brief"])
        print("[brief-it: %d -> %d tok, %s%% saved]" % (
            r["orig_tokens"], r["brief_tokens"], r["saved_pct"]))


def cmd_simple(a):
    import json as _json
    if a.file:
        code = open(a.file, encoding="utf-8").read()
    else:
        code = sys.stdin.read()
    r = cv.simplicity_check(code)
    if a.json:
        print(_json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print("verdict: %s (score %d, %d lines, %d functions)" % (
            r["verdict"], r["score"], r["counts"]["lines"], r["counts"]["functions"]))
        for f in r["findings"]:
            print("- [%s] %s" % (f["kind"], f["msg"]))
        if not r["findings"]:
            print("- clean. ship it.")


def cmd_delete_game(a):
    payload, err = cv.delete_game(a.game, a.yes, a.name, a.delete)
    if err:
        print("FAILED: %s" % err)
        raise SystemExit(1)
    print("OK: removed %s (%d bytes) -> .trash/ (default channel is protected)" % (
        payload["trashed"], payload["bytes_reclaimed"]))


def cmd_tree(a):
    import json as _json
    t, _ = cv.load_tree(a.game)
    if t is None:
        print("Unknown channel.")
        return
    nodes = sum(1 for _ in cv._walk_tree(t.get("roots", [])))
    print("# TREE %s (v%s, %d nodes, by %s %s)" % (
        a.game, t.get("version", 0), nodes, t.get("updated_by", "?"), t.get("updated_at", "")))
    print(cv.render_tree_text(t.get("roots", [])))


def cmd_tree_set(a):
    import json as _json
    doc = _json.load(open(a.file, encoding="utf-8"))
    roots = doc.get("roots", doc) if isinstance(doc, dict) else doc
    current, _ = cv.load_tree(a.game)
    exp = current.get("version", 0) if current else 0
    t, err = cv.save_tree(a.game, a.agent, roots, exp)
    if err:
        print("FAILED: %s" % err)
        raise SystemExit(1)
    print("OK: tree v%d (%d nodes) by %s" % (
        t["version"], sum(1 for _ in cv._walk_tree(t["roots"])), a.agent))


def main():
    p = argparse.ArgumentParser(description="Token-efficient reader for AI INTERCONNECTION CENTER")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_game_arg(sp):
        # allow --game after the subcommand too (argparse only accepts
        # parent optionals before the subcommand otherwise)
        sp.add_argument("--game", default=DEFAULT_GAME)
    import functools
    _orig_add_parser = sub.add_parser
    @functools.wraps(_orig_add_parser)
    def patched(*a, **k):
        sp = _orig_add_parser(*a, **k)
        add_game_arg(sp)
        return sp
    sub.add_parser = patched  # type: ignore
    sub.add_parser("games").set_defaults(fn=cmd_games)
    sub.add_parser("stats").set_defaults(fn=cmd_stats)
    d = sub.add_parser("digest"); d.add_argument("--per-msg", type=int, default=180)
    d.add_argument("--max", type=int, default=0); d.add_argument("--agent", default=None)
    d.add_argument("--type", default="all"); d.add_argument("--q", default=None); d.set_defaults(fn=cmd_digest)
    b = sub.add_parser("brief"); b.add_argument("--recent", type=int, default=3); b.set_defaults(fn=cmd_brief)
    sub.add_parser("guide").set_defaults(fn=cmd_guide)
    sub.add_parser("tree").set_defaults(fn=cmd_tree)
    ts = sub.add_parser("tree-set")
    ts.add_argument("--file", required=True, help="JSON file with {roots:[...]} or bare [...]")
    ts.add_argument("--agent", required=True, help="Your agent name (see /guide roster)")
    ts.set_defaults(fn=cmd_tree_set)
    dg = sub.add_parser("delete-game")
    dg.add_argument("--yes", action="store_true", help="Step 1/3: I understand the project leaves the app")
    dg.add_argument("--name", default="", help="Step 2/3: retype the EXACT channel file name")
    dg.add_argument("--delete", default="", help="Step 3/3: type DELETE")
    dg.set_defaults(fn=cmd_delete_game)
    km = sub.add_parser("kmap")
    km.add_argument("--ask", default="", help="Question -> scoped subgraph")
    km.add_argument("--path", nargs=2, metavar=("A", "B"), help="Shortest path between two nodes")
    km.add_argument("--explain", default="", help="Explain one node + connections")
    km.set_defaults(fn=cmd_kmap)
    bi = sub.add_parser("brief-it")
    bi.add_argument("--text", default="", help="Text to condense (or stdin)")
    bi.add_argument("--file", default="", help="File to condense")
    bi.add_argument("--json", action="store_true", help="Machine output with savings stats")
    bi.set_defaults(fn=cmd_brief_it)
    sp = sub.add_parser("simple")
    sp.add_argument("--file", default="", help="Code file to review (or stdin)")
    sp.add_argument("--json", action="store_true", help="Machine output")
    sp.set_defaults(fn=cmd_simple)
    t = sub.add_parser("tail"); t.add_argument("n", type=int)
    t.add_argument("--agent", default=None); t.add_argument("--q", default=None)
    t.add_argument("--type", default="all"); t.set_defaults(fn=cmd_tail)
    g = sub.add_parser("get"); g.add_argument("--from-line", type=int, required=True)
    g.add_argument("--max-chars", type=int, default=4000); g.set_defaults(fn=cmd_get)
    s = sub.add_parser("search"); s.add_argument("keyword")
    s.add_argument("--max", type=int, default=10); s.set_defaults(fn=cmd_search)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
