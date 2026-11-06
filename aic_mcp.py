"""MCP server (stdio) for the AI Interconnection Center.

Gives any MCP client - Claude Code, Codex CLI, Cursor, Cline, Windsurf... -
native tools for the hub, so the agent never has to hand-write HTTP calls.

  claude mcp add aic -e AIC_AGENT=Claude -e AIC_GAME="AI INTERCONNECTION CENTER - My Game.txt" -- python /path/aic_mcp.py

Env: AIC_AGENT (who this agent is, required), AIC_GAME (default project),
     AIC_URL (default http://127.0.0.1:19843), AIC_KEY (only if access keys are on).
The hub must be running (python channel_viewer.py). Standard library only.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

URL = (os.environ.get("AIC_URL") or "http://127.0.0.1:%s" % os.environ.get("AIC_PORT", "19843")).rstrip("/")
AGENT = os.environ.get("AIC_AGENT", "")
GAME = os.environ.get("AIC_GAME", "")
KEY = os.environ.get("AIC_KEY", "")
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
_cursor = {}

GAME_PROP = {"game": {"type": "string", "description": "Channel id (defaults to AIC_GAME). Use aic_projects to list them."}}

TOOLS = [
    {"name": "aic_sync",
     "description": "Call at the start of EVERY turn. Returns new team messages since your last sync (ones addressed to you in full), "
                    "your open tasks, files other agents have claimed (do not edit those), team status and project rules. "
                    "The cursor is remembered for you.",
     "inputSchema": {"type": "object", "properties": dict(GAME_PROP, reset={"type": "boolean", "description": "Start over from the latest messages"})}},
    {"name": "aic_post",
     "description": "Post to the team channel. After every change post type 'Change Notice' with WHAT changed, FILES touched, "
                    "and VERIFIED (how) / NOT verified (what). Use @Name to address a teammate, @Human for the human.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": dict(GAME_PROP,
         text={"type": "string"},
         type={"type": "string", "enum": ["General", "Change Notice", "Decision", "Question", "Blocker", "Handoff", "Review", "Status"]},
         to={"type": "array", "items": {"type": "string"}, "description": "Recipients (same as @mentions)"})}},
    {"name": "aic_claim",
     "description": "Claim files or folders BEFORE editing them so no other agent edits them at the same time. "
                    "Fails with the current holder if someone else has an overlapping claim. Claim again to renew.",
     "inputSchema": {"type": "object", "required": ["paths"], "properties": dict(GAME_PROP,
         paths={"type": "array", "items": {"type": "string"}, "description": "Files, folders (src/ui/) or globs"},
         note={"type": "string", "description": "What you are doing"},
         ttl_min={"type": "integer", "description": "Lease in minutes (default 30, max 480)"},
         task={"type": "string", "description": "Task id, e.g. T3"})}},
    {"name": "aic_release",
     "description": "Release your claims when you are done editing.",
     "inputSchema": {"type": "object", "properties": dict(GAME_PROP,
         all={"type": "boolean"}, id={"type": "string"}, paths={"type": "array", "items": {"type": "string"}})}},
    {"name": "aic_tasks",
     "description": "List tasks on the project board.",
     "inputSchema": {"type": "object", "properties": dict(GAME_PROP,
         mine={"type": "boolean", "description": "Only tasks assigned to you"},
         status={"type": "string", "description": "Comma list: todo,doing,review,done"})}},
    {"name": "aic_task_create",
     "description": "Create a task on the board (optionally assigned to an agent).",
     "inputSchema": {"type": "object", "required": ["title"], "properties": dict(GAME_PROP,
         title={"type": "string"}, detail={"type": "string"}, assignee={"type": "string"},
         paths={"type": "array", "items": {"type": "string"}})}},
    {"name": "aic_task_update",
     "description": "Move a task (todo -> doing -> review -> done) or reassign it.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": dict(GAME_PROP,
         id={"type": "string"}, status={"type": "string", "enum": ["todo", "doing", "review", "done"]},
         assignee={"type": "string"}, detail={"type": "string"})}},
    {"name": "aic_tree",
     "description": "Read the project tree: systems, files and invariants. Read it when aic_sync says it changed.",
     "inputSchema": {"type": "object", "properties": dict(GAME_PROP)}},
    {"name": "aic_read",
     "description": "Read history: one message in full (from_line + limit 1), the last N messages (tail), or a window.",
     "inputSchema": {"type": "object", "properties": dict(GAME_PROP,
         from_line={"type": "integer"}, limit={"type": "integer"}, tail={"type": "integer"},
         agent={"type": "string"}, type={"type": "string"})}},
    {"name": "aic_search",
     "description": "Search the whole project history for a keyword (newest last).",
     "inputSchema": {"type": "object", "required": ["q"], "properties": dict(GAME_PROP,
         q={"type": "string"}, tail={"type": "integer", "description": "Max hits (default 10)"})}},
    {"name": "aic_status",
     "description": "Set your one-line status on the human's dashboard (e.g. 'running tests').",
     "inputSchema": {"type": "object", "required": ["doing"], "properties": dict(GAME_PROP, doing={"type": "string"})}},
    {"name": "aic_projects",
     "description": "List project channels on this hub.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class HubError(Exception):
    pass


def hub(path, body=None):
    headers = {"Content-Type": "application/json"}
    if KEY:
        headers["X-AIC-Key"] = KEY
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(URL + path, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8")
            return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error")
        except ValueError:
            msg = e.reason
        raise HubError("HTTP %d: %s" % (e.code, msg))
    except urllib.error.URLError:
        raise HubError("The hub is not running at %s (start it with: python channel_viewer.py)." % URL)


def call(name, args):
    game = args.get("game") or GAME
    if name == "aic_projects":
        return "\n".join("%s  (%d messages)  id: %s" % (g["label"], g["messages"], g["id"]) for g in hub("/games"))
    if not AGENT:
        raise HubError("Set AIC_AGENT to your agent name in this MCP server's env.")
    q = lambda **kw: urllib.parse.urlencode({k: v for k, v in kw.items() if v not in (None, "")})  # noqa: E731
    if name == "aic_sync":
        since = None if args.get("reset") else _cursor.get(game)
        s = hub("/sync?" + q(game=game, agent=AGENT, since=since, format="json"))
        _cursor[game] = s["cursor"]
        return _render_sync(s)
    if name == "aic_post":
        r = hub("/post", {"game": game, "agent": AGENT, "text": args["text"], "type": args.get("type"), "to": args.get("to")})
        extra = (" %s will reply in the channel." % ", ".join(r["triggered"])) if r.get("triggered") else ""
        return "Posted L%d [%s].%s" % (r["line"], r["type"], extra)
    if name == "aic_claim":
        r = hub("/claim", {"game": game, "agent": AGENT, "paths": args["paths"], "note": args.get("note", ""),
                           "ttl_min": args.get("ttl_min", 30), "task": args.get("task", "")})
        c = r["claim"]
        return "Claimed %s as %s until %s." % (", ".join(c["paths"]), c["id"], c["expires_at"])
    if name == "aic_release":
        body = {"game": game, "agent": AGENT}
        for k in ("all", "id", "paths"):
            if args.get(k):
                body[k] = args[k]
        if len(body) == 2:
            body["all"] = True
        r = hub("/release", body)
        return "Released " + "; ".join("%s %s" % (c["id"], ", ".join(c["paths"])) for c in r["released"])
    if name == "aic_tasks":
        return hub("/tasks?" + q(game=game, format="text", assignee=AGENT if args.get("mine") else None,
                                 status=args.get("status")))
    if name == "aic_task_create":
        t = hub("/tasks", dict({"game": game, "agent": AGENT}, **{k: args[k] for k in ("title", "detail", "assignee", "paths") if k in args}))["task"]
        return "Created %s: %s" % (t["id"], t["title"])
    if name == "aic_task_update":
        t = hub("/tasks/update", dict({"game": game, "agent": AGENT}, **{k: args[k] for k in ("id", "status", "assignee", "detail") if k in args}))["task"]
        return "%s is now [%s], assignee %s." % (t["id"], t["status"], t["assignee"] or "none")
    if name == "aic_tree":
        return hub("/tree?" + q(game=game, format="text"))
    if name == "aic_read":
        return hub("/messages?" + q(game=game, from_line=args.get("from_line"), limit=args.get("limit"),
                                    tail=args.get("tail") or (None if args.get("from_line") else 5),
                                    agent=args.get("agent"), type=args.get("type"), max_chars=12000))
    if name == "aic_search":
        return hub("/messages?" + q(game=game, q=args["q"], tail=args.get("tail") or 10, max_chars=12000)) or "No matches."
    if name == "aic_status":
        hub("/status", {"game": game, "agent": AGENT, "doing": args["doing"]})
        return "Status set."
    raise HubError("Unknown tool %s" % name)


def _render_sync(s):
    # the server renders the same text; keep this client dumb and consistent
    lines = ["SYNC cursor=%d%s" % (s["cursor"], " (first sync: last 5 as digest)" if s["first_sync"] else "")]
    if s["rules"]:
        lines += ["PROJECT RULES:", s["rules"].strip()]
    if s["team"]:
        lines.append("TEAM: " + "; ".join("%s %ss ago%s" % (t["agent"], t["ago_s"], (" - " + t["doing"]) if t["doing"] else "") for t in s["team"]))
    for c in s["claims_by_others"]:
        lines.append("CLAIMED BY %s (do not edit): %s%s" % (c["agent"], ", ".join(c["paths"]), (' "%s"' % c["note"]) if c["note"] else ""))
    if s["your_claims"]:
        lines.append("YOUR CLAIMS: " + "; ".join("%s %s" % (c["id"], ", ".join(c["paths"])) for c in s["your_claims"]))
    for t in s["your_tasks"]:
        lines.append("YOUR TASK %s [%s] %s" % (t["id"], t["status"], t["title"]))
    if not s["your_tasks"] and s["unassigned_todo"]:
        lines.append("%d unassigned todo tasks (aic_tasks)" % s["unassigned_todo"])
    if s["tree"]["changed"]:
        lines.append("TREE CHANGED to v%d by %s: read it with aic_tree" % (s["tree"]["version"], s["tree"]["updated_by"]))
    if not s["messages"]:
        lines.append("No new messages.")
    for m in s["messages"]:
        head = "%sL%d %s [%s]" % (">> " if m["addressed"] else "", m["line"], m["agent"], m["type"])
        lines.append(("%s\n%s" if m["full"] else "%s %s") % (head, m["text"]))
    return "\n".join(lines)


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            reply(None, error={"code": -32700, "message": "Parse error"})
            continue
        method, msg_id = msg.get("method"), msg.get("id")
        if msg_id is None:
            continue  # notification (initialized, cancelled...)
        if method == "initialize":
            asked = (msg.get("params") or {}).get("protocolVersion")
            reply(msg_id, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "ai-interconnection-center", "version": "3.0.0"},
                           "instructions": "Team coordination hub. Call aic_sync at the start of every turn, aic_claim before "
                                           "editing files, aic_post a Change Notice after each change, aic_release when done."})
        elif method == "ping":
            reply(msg_id, {})
        elif method == "tools/list":
            reply(msg_id, {"tools": TOOLS})
        elif method == "tools/call":
            p = msg.get("params") or {}
            try:
                text = call(p.get("name"), p.get("arguments") or {})
                reply(msg_id, {"content": [{"type": "text", "text": str(text)}], "isError": False})
            except HubError as e:
                reply(msg_id, {"content": [{"type": "text", "text": str(e)}], "isError": True})
            except (KeyError, TypeError) as e:
                reply(msg_id, {"content": [{"type": "text", "text": "Bad arguments: %s" % e}], "isError": True})
        else:
            reply(msg_id, error={"code": -32601, "message": "Method not found: %s" % method})


if __name__ == "__main__":
    main()
