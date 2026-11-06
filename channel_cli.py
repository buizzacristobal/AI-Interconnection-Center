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

Coordination (talks to the running hub; identity from --agent or AIC_AGENT):
  python channel_cli.py sync  [--game "<file>"]            new messages, your tasks, others' claims (cursor kept for you)
  python channel_cli.py post  --type "Change Notice" --text "WHAT: .. FILES: .. VERIFIED: .."   (or --file / stdin)
  python channel_cli.py claim src/a.lua src/ui/ --note "why" [--ttl 30]
  python channel_cli.py release --all | --id C3 | <paths>
  python channel_cli.py tasks [--mine] [--status todo,doing]
  python channel_cli.py task-add "Title" [--assign Codex] [--detail ..] [--paths a,b]
  python channel_cli.py task-set T3 --status review [--assign Cline]
  python channel_cli.py status "running the test suite"
Env: AIC_AGENT, AIC_GAME, AIC_KEY, AIC_URL (default http://127.0.0.1:19843)

Protocol for AIs (cheapest first): sync every turn; stats -> digest -> brief -> tail/search for history.
Never `cat` the raw .txt on channels over ~20KB; you will burn context.
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
import channel_viewer as cv

DEFAULT_GAME = os.environ.get("AIC_GAME") or "AI INTERCONNECTION CENTER.txt"
HUB = os.environ.get("AIC_URL") or "http://127.0.0.1:%d" % cv.PORT


def load(game):
    if cv.game_path(game) is None:
        raise SystemExit("Unknown channel %r. Channels: %s" % (game, ", ".join(cv.channel_ids())))
    items, chars = cv.load_items(game)
    return chars, items


# ---- hub calls (coordination commands go through the server so presence,
# hosted-agent replies and live updates all work)

def hub(path, body=None):
    headers = {"Content-Type": "application/json"}
    if os.environ.get("AIC_KEY"):
        headers["X-AIC-Key"] = os.environ["AIC_KEY"]
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(HUB + path, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error")
        except ValueError:
            msg = e.reason
        print("FAILED (%d): %s" % (e.code, msg))
        raise SystemExit(1)
    except urllib.error.URLError:
        print("The hub is not running at %s. Start it: python channel_viewer.py" % HUB)
        raise SystemExit(2)


def need_agent(a):
    agent = a.agent or os.environ.get("AIC_AGENT")
    if not agent:
        print("Who are you? Pass --agent <Name> or set AIC_AGENT.")
        raise SystemExit(2)
    return agent


def cursor_file(agent, game):
    h = hashlib.sha1(game.encode("utf-8")).hexdigest()[:10]
    return os.path.join(cv.store.STATE_DIR, "cursors", "%s_%s.txt" % (agent.lower(), h))


def cmd_sync(a):
    agent = need_agent(a)
    cf = cursor_file(agent, a.game)
    since = a.since
    if since is None and not a.reset:
        try:
            since = int(open(cf).read().strip())
        except (OSError, ValueError):
            since = None
    q = {"game": a.game, "agent": agent, "format": "json"}
    if since is not None:
        q["since"] = since
    s = hub("/sync?" + urllib.parse.urlencode(q))
    os.makedirs(os.path.dirname(cf), exist_ok=True)
    with open(cf, "w") as f:
        f.write(str(s["cursor"]))
    if a.json:
        print(json.dumps(s, ensure_ascii=False, indent=1))
    else:
        print(cv.render_sync_text(s))


def read_text_arg(a):
    if a.file:
        return open(a.file, encoding="utf-8").read()
    if a.text:
        return a.text
    return sys.stdin.read()


def cmd_post(a):
    body = {"game": a.game, "agent": need_agent(a), "text": read_text_arg(a), "type": a.type}
    if a.to:
        body["to"] = [x.strip() for x in a.to.split(",") if x.strip()]
    r = hub("/post", body)
    print("OK: posted L%d as %s [%s]%s" % (r["line"], r["agent"], r["type"],
                                         (" - %s will reply" % ", ".join(r["triggered"])) if r.get("triggered") else ""))


def cmd_claim(a):
    r = hub("/claim", {"game": a.game, "agent": need_agent(a), "paths": a.paths, "note": a.note, "ttl_min": a.ttl})
    c = r["claim"]
    print("OK: %s holds %s until %s" % (c["id"], ", ".join(c["paths"]), c["expires_at"]))


def cmd_release(a):
    body = {"game": a.game, "agent": need_agent(a)}
    if a.all:
        body["all"] = True
    elif a.id:
        body["id"] = a.id
    elif a.paths:
        body["paths"] = a.paths
    else:
        print("Say what to release: --all, --id C3, or paths.")
        raise SystemExit(2)
    r = hub("/release", body)
    print("OK: released " + "; ".join("%s %s" % (c["id"], ", ".join(c["paths"])) for c in r["released"]))


def cmd_tasks(a):
    q = {"game": a.game, "format": "text"}
    if a.mine:
        q["assignee"] = need_agent(a)
    if a.status:
        q["status"] = a.status
    print(hub("/tasks?" + urllib.parse.urlencode(q)))


def cmd_task_add(a):
    body = {"game": a.game, "agent": need_agent(a), "title": a.title, "detail": a.detail or "",
            "assignee": a.assign or "", "paths": a.paths or ""}
    t = hub("/tasks", body)["task"]
    print("OK: %s created%s" % (t["id"], (" for " + t["assignee"]) if t["assignee"] else ""))


def cmd_task_set(a):
    body = {"game": a.game, "agent": need_agent(a), "id": a.id}
    if a.status:
        body["status"] = a.status
    if a.assign is not None:
        body["assignee"] = a.assign
    t = hub("/tasks/update", body)["task"]
    print("OK: %s [%s] %s -> %s" % (t["id"], t["status"], t["title"], t["assignee"] or "unassigned"))


def cmd_status(a):
    hub("/status", {"game": a.game, "agent": need_agent(a), "doing": " ".join(a.doing)})
    print("OK")


def cmd_games(_a):
    for g in cv.list_games():
        print("%(messages)4d msgs ~%(est_tokens)6d tok %(bytes)7d B  %(id)s" % g)


def cmd_stats(a):
    chars, items = load(a.game)
    s = cv.build_stats(items, chars)
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
    chars, items = load(a.game)
    b = cv.build_brief(items, chars, recent=a.recent, budget_chars=16000)
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
    print(cv.guide_text())


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
    def add_agent(sp):
        sp.add_argument("--agent", default=None, help="Your agent name (or set AIC_AGENT)")
        return sp
    sy = add_agent(sub.add_parser("sync"))
    sy.add_argument("--since", type=int, default=None)
    sy.add_argument("--reset", action="store_true", help="Forget the stored cursor")
    sy.add_argument("--json", action="store_true")
    sy.set_defaults(fn=cmd_sync)
    po = add_agent(sub.add_parser("post"))
    po.add_argument("--text", default="")
    po.add_argument("--file", default="")
    po.add_argument("--type", default=None, help="Change Notice, Decision, Question, Blocker, Handoff, Review, Status")
    po.add_argument("--to", default="", help="Comma-separated recipients")
    po.set_defaults(fn=cmd_post)
    cl = add_agent(sub.add_parser("claim"))
    cl.add_argument("paths", nargs="+")
    cl.add_argument("--note", default="")
    cl.add_argument("--ttl", type=int, default=30, help="Minutes (1-480)")
    cl.set_defaults(fn=cmd_claim)
    rl = add_agent(sub.add_parser("release"))
    rl.add_argument("paths", nargs="*")
    rl.add_argument("--all", action="store_true")
    rl.add_argument("--id", default="")
    rl.set_defaults(fn=cmd_release)
    tk = add_agent(sub.add_parser("tasks"))
    tk.add_argument("--mine", action="store_true")
    tk.add_argument("--status", default="")
    tk.set_defaults(fn=cmd_tasks)
    ta = add_agent(sub.add_parser("task-add"))
    ta.add_argument("title")
    ta.add_argument("--assign", default="")
    ta.add_argument("--detail", default="")
    ta.add_argument("--paths", default="")
    ta.set_defaults(fn=cmd_task_add)
    tse = add_agent(sub.add_parser("task-set"))
    tse.add_argument("id")
    tse.add_argument("--status", default="")
    tse.add_argument("--assign", default=None)
    tse.set_defaults(fn=cmd_task_set)
    st = add_agent(sub.add_parser("status"))
    st.add_argument("doing", nargs="+")
    st.set_defaults(fn=cmd_status)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
