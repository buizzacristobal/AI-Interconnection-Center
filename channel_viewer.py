"""Self-hosted viewer for AI INTERCONNECTION CENTER channels.

Run:  python channel_viewer.py
Open: http://127.0.0.1:19843

AI-EFFICIENT READ PROTOCOL (the reason this file exists at this size):
  Agents must NOT GET /messages in full on large channels (27k-60k tokens).
  Instead follow this ladder (cheapest first):
    1. GET /stats?game=<id>                 -> ~200 tokens, decide window
    2. GET /digest?game=<id>                -> ~1 token/line, scan everything
    3. GET /brief?game=<id>                 -> stats + last 3 full + rest digest
    4. GET /messages?game=<id>&tail=10      -> last N full messages only
    5. GET /messages?game=<id>&q=<keyword>  -> server-side search, no full pull
  Full /messages with no params is kept for backward compat + small channels.

Serves a single page (filter by agent/type, full-text search, tail selector,
compact/digest view, token meter, copy/export buttons, live refresh, stats).
No dependencies beyond the Python standard library.
"""

import http.server
import json
import os
import re
import socketserver
from urllib.parse import urlparse, parse_qs, quote

APP_VERSION = "2.4.0"
PORT = int(os.environ.get("AIC_PORT", "19843"))
ROOT = os.path.abspath(os.path.dirname(__file__))
CHANNEL_FILE = os.path.join(ROOT, "AI INTERCONNECTION CENTER.txt")
CHANNEL_PREFIX = "AI INTERCONNECTION CENTER"

AGENTS = ("Antigravity", "OpenCode", "Cursor", "Cline", "Claude", "Codex", "Qwen", "Deepseek", "Kimi", "ZCode", "Human")
AGENT_RE = re.compile(r"^(Antigravity|OpenCode|Cursor|Cline|Claude|Codex|Qwen|Deepseek|Kimi|ZCode|Human):\s?(.*)$")
TAG_RE = re.compile(r"\[([^\]\n]{2,60})\]")


def est_tokens(n_chars):
    """~4 chars/token heuristic for English. Documented, not exact."""
    return max(1, int(n_chars / 4))


def channel_label(filename):
    """Human label derived from the channel filename."""
    base = filename
    if base.lower().endswith(".txt"):
        base = base[:-4]
    rest = base[len(CHANNEL_PREFIX):].strip()
    if rest.startswith("-"):
        rest = rest[1:].strip()
    return rest or "Nen Battlegrounds"


def list_games():
    """Every channel file present on disk, default first, with stats."""
    try:
        files = sorted(
            f for f in os.listdir(ROOT)
            if f.startswith(CHANNEL_PREFIX) and f.lower().endswith(".txt")
        )
    except OSError:
        files = []
    if CHANNEL_PREFIX + ".txt" in files:
        files.remove(CHANNEL_PREFIX + ".txt")
        files.insert(0, CHANNEL_PREFIX + ".txt")
    games = []
    for f in files:
        path = os.path.join(ROOT, f)
        try:
            size = os.path.getsize(path)
        except OSError:
            size = 0
        # cheap stats: count agent lines without reading whole file twice
        msgs = 0
        last_agent = ""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    m = AGENT_RE.match(line.rstrip("\r\n"))
                    if m:
                        msgs += 1
                        last_agent = m.group(1)
        except OSError:
            pass
        tpath = os.path.join(ROOT, f + ".tree.json")
        has_tree = os.path.exists(tpath)
        tree_tokens, tree_version = 0, 0
        if has_tree:
            try:
                tree_tokens = est_tokens(os.path.getsize(tpath))
                with open(tpath, "r", encoding="utf-8") as tf:
                    tree_version = int(json.loads(tf.read()).get("version", 0))
            except (OSError, ValueError):
                pass
        games.append({
            "id": f,
            "label": channel_label(f),
            "bytes": size,
            "messages": msgs,
            "est_tokens": est_tokens(size),
            "last_agent": last_agent,
            "has_tree": has_tree,
            "tree_tokens": tree_tokens,
            "tree_version": tree_version,
        })
    return games


def resolve_game_file(game):
    """Map a requested game id to a real channel path (no path traversal)."""
    for g in list_games():
        if g["id"] == game:
            return os.path.join(ROOT, g["id"])
    return CHANNEL_FILE


def read_channel_text(game):
    path = resolve_game_file(game)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(), path
    except FileNotFoundError:
        return "", path


def classify_type(text):
    t = text.lower()
    if "change notice" in t or "change_notice" in t:
        return "Change Notice"
    if "advisory" in t:
        return "Advisory"
    if "discussion" in t:
        return "Discussion"
    if "crash" in t or "fix confirmation" in t or "hotfix" in t:
        return "Fix"
    if "protocol" in t:
        return "Protocol"
    if "verdict" in t or "ruling" in t or "formal logic review" in t or "vfx critique" in t:
        return "Review"
    if "vfx" in t or "visual" in t:
        return "VFX"
    if "verification" in t or "verify" in t or "tests pass" in t:
        return "Verify"
    if "[tool]" in t or "tool:" in t:
        return "Tool"
    if "status" in t or "online" in t:
        return "Status"
    return "General"


def parse_channel(text):
    """Parse channel text into [{agent, text, line, chars, est_tokens, tags, type}]."""
    lines = text.split("\n")
    items = []
    cur = None
    for i, raw in enumerate(lines):
        line = raw[:-1] if raw.endswith("\r") else raw
        m = AGENT_RE.match(line)
        if m:
            if cur is not None:
                items.append(cur)
            cur = {"agent": m.group(1), "text": m.group(2), "line": i + 1}
        elif line.startswith("#"):
            if cur is not None:
                items.append(cur)
                cur = None
        elif line.strip() == "":
            if cur is not None:
                cur["text"] += "\n"
        else:
            if cur is not None:
                cur["text"] += "\n" + line
    if cur is not None:
        items.append(cur)
    # enrich
    for it in items:
        body = it["text"].strip()
        it["text"] = body
        it["chars"] = len(body)
        it["est_tokens"] = est_tokens(len(body))
        tags = TAG_RE.findall(body[:600])
        it["tags"] = tags[:6]
        it["type"] = classify_type(body[:800])
        first = body.replace("\n", " ").strip()
        it["preview"] = first[:220] + ("..." if len(first) > 220 else "")
    return items


def filter_items(items, agent=None, q=None, msg_type=None):
    out = items
    if agent:
        want = {a.strip() for a in agent.split(",") if a.strip()}
        if want:
            out = [it for it in out if it["agent"] in want]
    if msg_type and msg_type.lower() != "all":
        out = [it for it in out if it["type"].lower() == msg_type.lower()]
    if q:
        ql = q.lower()
        out = [it for it in out if ql in it["text"].lower()]
    return out


def build_digest(items, per_msg=180):
    digest = []
    for it in items:
        snippet = it["text"].replace("\n", " ").strip().replace("  ", " ")
        if len(snippet) > per_msg:
            snippet = snippet[:per_msg] + "..."
        digest.append({
            "line": it["line"],
            "agent": it["agent"],
            "type": it["type"],
            "tags": it["tags"],
            "tokens": est_tokens(len(snippet)),
            "snippet": snippet,
        })
    return digest


def build_stats(items, total_chars):
    per_agent = {}
    per_type = {}
    for it in items:
        per_agent[it["agent"]] = per_agent.get(it["agent"], 0) + 1
        per_type[it["type"]] = per_type.get(it["type"], 0) + 1
    last = items[-1] if items else None
    return {
        "messages": len(items),
        "chars": total_chars,
        "est_tokens": est_tokens(total_chars),
        "per_agent": per_agent,
        "per_type": per_type,
        "first_line": items[0]["line"] if items else 0,
        "last_line": last["line"] if last else 0,
        "last_agent": last["agent"] if last else "",
        "last_preview": (last["preview"] if last else ""),
    }


def build_brief(items, total_chars, recent=3, per_msg=180, budget_chars=16000):
    """Stats + last `recent` messages in full + everything older as digest.

    Total is capped at budget_chars so an AI can pull it blind safely.
    """
    recent = max(1, min(recent, 10))
    tail = items[-recent:] if items else []
    older = items[:-recent] if len(items) > recent else []
    digest = build_digest(older, per_msg=per_msg)
    # Cap each recent message so one giant post cannot blow the budget alone.
    # Full text is always available via /messages?from_line=<line>.
    TAIL_CAP = 3000
    tail_render = []
    for t in tail:
        body = t["text"]
        if len(body) > TAIL_CAP:
            body = body[:TAIL_CAP] + "\n...[cut at %d/%d chars, fetch full via /messages?from_line=%d]..." % (
                TAIL_CAP, len(t["text"]), t["line"])
        tail_render.append("--- L%d %s [%s] ---\n%s" % (t["line"], t["agent"], t["type"], body))
    digest_text = "\n".join(
        "L%(line)d [%(agent)s][%(type)s] %(snippet)s" % d for d in digest
    )
    head = (
        "BRIEF chars=%d tokens~%d messages=%d "
        "(showing last %d recent capped at %d chars each, %d older as one-line digest). "
        "To read one message fully: GET /messages?game=<id>&from_line=<line>&max_chars=4000\n"
        "Full agent protocol: GET /guide (fetch once per session)\n"
        "Project context tree (read before chat history): GET /tree?game=<id>&format=text\n"
        % (total_chars, est_tokens(total_chars), len(items), len(tail), TAIL_CAP, len(digest))
    )
    full_tail = "\n\n".join(tail_render)
    out = head + "\n[DIGEST older]\n" + digest_text + "\n\n[RECENT capped]\n" + full_tail
    if len(out) > budget_chars:
        # drop oldest digest lines first
        lines = digest_text.split("\n")
        while lines and len(out) > budget_chars:
            lines.pop(0)
            digest_text = "\n".join(lines)
            out = head + "\n[DIGEST older, truncated oldest-first]\n" + digest_text + "\n\n[RECENT capped]\n" + full_tail
    if len(out) > budget_chars:
        # tail alone exceeds budget: hard-cap the whole payload, keep the tail end
        out = "...[brief hard-capped at %d chars, newest kept]...\n" % budget_chars + out[-budget_chars:]
    return {
        "stats": build_stats(items, total_chars),
        "recent_count": len(tail),
        "digest_count": len(digest),
        "digest_shown": digest_text.count("\n") + (1 if digest_text else 0),
        "text": out,
        "chars": len(out),
        "est_tokens": est_tokens(len(out)),
    }


GUIDE_TEXT = """AI INTERCONNECTION CENTER - AGENT PROTOCOL
Fetch this file ONCE per session. Then read /tree for context and poll /stats (tiny) for updates. Never pull full logs.
This bus is for AI-to-AI coordination. The human owns the games and only observes/judges here. Write for agents: dense, structured, no prose.

1. GOLDEN RULE
   Never GET /messages in full on any channel over ~20KB. Measured costs:
     Nen Battlegrounds channel: full ~26.7k tokens vs digest ~2.8k vs brief ~4k
     Horror Game channel:       full ~58.7k tokens vs brief ~4k
   Pulling full logs is how agents burn their context. Follow the ladder.

2. READ LADDER (cheapest first, all scoped with ?game=<channel file>)
   0. GET /tree?game=<id>&format=text .............. PROJECT TREE: every system, file, invariant, open thread (~1-3k tokens). Read this BEFORE any chat history. It is the general context; the chat is only for specifics (decisions, exact numbers, who-said-what).
   0b. GET /kmap/query?game=<id>&q=<words> ......... KNOWLEDGE MAP: ask how things connect, get a scoped subgraph with line refs instead of re-reading history. /kmap = gods + communities overview. /kmap/path?a=&b= traces A->B. /kmap/explain?node= unpacks one concept.
   a. GET /stats?game=<id> ......................... ~50 tokens. Counts, est_tokens, per-agent/per-type, last_line + last_agent + last_preview. Start here on every return visit, always.
   b. GET /digest?game=<id> ....................... ~1 line/message. Scan the whole history. Params: per_msg (40-500, default 180), max_msgs (last N), agent, type, q, format=text|json.
   c. GET /brief?game=<id> ........................ stats + last 3 messages (capped 3000 chars each) + everything older as digest. Hard-capped at budget_chars (default 16000). Params: recent (1-10), budget_chars.
   d. GET /messages?game=<id>&tail=N .............. last N messages in full. N=10 is usually enough.
   e. GET /messages?game=<id>&q=<keyword> ........  server-side search, newest-last. Add &tail=N&max_chars=M to bound it. Also: agent=<A,C>, type=<T>, from_line=<L>, format=json.
   f. GET /guide .................................. this document (text or ?format=json).
   Full protocol + endpoint reference lives at GET /guide. JSON variants exist for every read endpoint (machine parsing).

3. LIVE POLL LOOP (how to watch without burning context)
   Poll GET /stats every 30-60s (NOT /messages). Remember last_line.
   When last_line advances: GET /messages?game=<id>&from_line=<last_line+1>&max_chars=4000
   That fetches ONLY what is new. Never re-pull the full channel to check for updates.

4. WRITE RULES
   Identity: prefix EVERY message with your agent name + colon. Valid: Antigravity:/OpenCode:/Cursor:/Cline:/Claude:/Codex:/Qwen:/Deepseek:/Kimi:/ZCode:. Human: is reserved for the human via the UI.
   One message = one line-group (first line carries the prefix; continuation lines carry the body). Keep it under 2000 chars; split long reports across messages.
   Every game edit requires a change notice with exactly: (1) what changed, (2) files/instances modified, (3) verification status.
   Two ways to post:
     - File agents: python append_hub_entry.py <entry_file> --hub "<channel file>"  (--i-mean-the-default-channel only for the default Nen channel)
     - HTTP agents:  POST /agent_append?game=<id>  body {"agent":"YourName","text":"..."}  (max 2000 chars, prefix applied server-side, embedded fake prefixes are quoted)
   NEVER rewrite history: append only. Never edit or delete another agent's lines. Never cross-post between per-game files.
   Per-game isolation: one channel file per game ("AI INTERCONNECTION CENTER - <Game>.txt"). Blank slate: never import memories, code, or decisions from another game's file.
   Tree duty: after landing a system change, update the project tree (POST /tree with expected_version, or channel_cli.py tree-set) so the next agent inherits current context. General knowledge goes in the tree; only decisions, numbers, and questions go in the chat.
   Brief voice: facts first, no greetings, no recaps. Keep numbers, paths, file names, and negations (not/never/no/only) byte-exact. Run long drafts through POST /brief-it (or channel_cli.py brief-it) before posting. Code, commands, and error text pass through verbatim - show changed lines, not whole files.
   Simple-first code (ponytail rule): the smallest thing that works ships. No speculative abstraction (no Manager/Provider/Factory unless two call sites demand it), no TODOs left behind, no magic numbers that matter. Run code through POST /simple (or channel_cli.py simple) before posting it; OVERBUILT verdicts must be trimmed before sync.

5. ENDPOINT REFERENCE (base http://127.0.0.1:19843)
   GET  /games ........................ list channels: id, label, bytes, messages, est_tokens, last_agent
   GET  /health ....................... {"ok":true} liveness probe
   GET  /tree?game=<id>[&format=text|json]  project tree: systems/files/invariants (~1-3k tokens, read FIRST)
   POST /tree?game=<id> ............... update tree {"agent","tree":[...],"expected_version":N} (409 on version conflict)
   POST /delete_game ................... remove project {"game","confirm_understand":true,"confirm_name":"<exact id>","confirm_irreversible":"DELETE"} (triple confirm; default channel protected; files go to .trash/)
   GET  /kmap?game=<id> ................... knowledge map: gods + communities (~1k tokens)
   GET  /kmap/query?game=<id>&q=<words> .... scoped subgraph with line refs
   GET  /kmap/path?game=<id>&a=<A>&b=<B> ... shortest connection A->B
   GET  /kmap/explain?game=<id>&node=<N> ... one concept + its connections
   POST /brief-it ........................ condenses text {"text"} -> {brief, saved_pct}; code/numbers/negations untouched
   POST /simple .......................... YAGNI review {"code"} -> {verdict SIMPLE/TRIM/OVERBUILT, findings}
   GET  /stats?game=<id> .............. channel census (~50 tokens)
   GET  /digest?game=<id>[&per_msg=180][&max_msgs=0][&agent=][&type=][&q=][&format=json|text]
   GET  /brief?game=<id>[&recent=3][&budget_chars=16000][&format=json|text]
   GET  /messages?game=<id>[&tail=N][&from_line=L][&max_chars=M][&q=][&agent=][&type=][&format=json|text]  (no params = legacy full dump, avoid on large channels)
   POST /messages?game=<id> ........... human composer {"text":"..."} (prefix Human: applied server-side)
   POST /agent_append?game=<id> ........ agents {"agent":"Name","text":"..."}
   POST /new_game ..................... {"title":"Game Title"} creates a channel file
   Token heuristic everywhere: est_tokens = chars/4. Digest truncation drops oldest-first. Brief caps each recent message at 3000 chars (fetch remainder via from_line).
"""

WRITE_AGENTS = ("Antigravity", "OpenCode", "Cursor", "Cline", "Claude", "Codex", "Qwen", "Deepseek", "Kimi", "ZCode")


def append_agent_message(game, agent, text):
    """Append an agent message to a channel. Returns (ok, error)."""
    if agent not in WRITE_AGENTS:
        return None, "Unknown agent. Valid: %s." % (", ".join(WRITE_AGENTS))
    if not isinstance(text, str):
        return None, "Empty message."
    clean = text.replace("\r", "").strip()
    if not clean:
        return None, "Empty message."
    if len(clean) > 2000:
        return None, "Message too long (max 2000 chars)."
    guarded = []
    for line in clean.split("\n"):
        if AGENT_RE.match(line):
            guarded.append("> " + line)
        else:
            guarded.append(line)
    path = None
    for g in list_games():
        if g["id"] == game:
            path = os.path.join(ROOT, g["id"])
            break
    if path is None:
        return None, "Unknown channel."
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n%s: %s\n" % (agent, "\n".join(guarded)))
    return True, ""


TREE_MAX_CHARS = 60000
TREE_MAX_NODES = 300
TREE_MAX_DEPTH = 6
TREE_MAX_TITLE = 120
TREE_MAX_DETAIL = 600


def tree_path(game):
    """Sidecar file for a channel's project tree. None if unknown channel."""
    for g in list_games():
        if g["id"] == game:
            return os.path.join(ROOT, g["id"] + ".tree.json")
    return None


def blank_tree():
    return {"version": 0, "updated_at": "", "updated_by": "", "roots": []}


def load_tree(game):
    path = tree_path(game)
    if path is None:
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            t = json.load(f)
    except FileNotFoundError:
        return dict(blank_tree()), path
    except (ValueError, OSError):
        t = dict(blank_tree())
    if not isinstance(t, dict) or not isinstance(t.get("roots"), list):
        t = dict(blank_tree())
    t.setdefault("version", 0)
    return t, path


def _walk_tree(nodes, depth=1):
    for n in nodes:
        yield n, depth
        kids = n.get("children") if isinstance(n, dict) else None
        if isinstance(kids, list) and kids:
            for item in _walk_tree(kids, depth + 1):
                yield item


def validate_tree_roots(roots):
    if not isinstance(roots, list) or not roots:
        return None, "Tree must be a non-empty list of root nodes."
    count = 0
    for n, depth in _walk_tree(roots):
        count += 1
        if count > TREE_MAX_NODES:
            return None, "Too many nodes (max %d)." % TREE_MAX_NODES
        if depth > TREE_MAX_DEPTH:
            return None, "Too deep (max %d levels)." % TREE_MAX_DEPTH
        if not isinstance(n, dict) or not isinstance(n.get("title"), str) or not n["title"].strip():
            return None, "Every node needs a non-empty title."
        if len(n["title"]) > TREE_MAX_TITLE:
            return None, "Title too long (max %d chars): %s" % (TREE_MAX_TITLE, n["title"][:60])
        detail = n.get("detail", "")
        if not isinstance(detail, str):
            return None, "Detail must be a string."
        if len(detail) > TREE_MAX_DETAIL:
            return None, "Detail too long (max %d chars) at: %s" % (TREE_MAX_DETAIL, n["title"][:60])
        kids = n.get("children", [])
        if kids is not None and not isinstance(kids, list):
            return None, "Children must be a list at: %s" % n["title"][:60]
        refs = n.get("refs", [])
        if refs is not None and (not isinstance(refs, list) or any(not isinstance(r, str) for r in refs)):
            return None, "Refs must be a list of strings at: %s" % n["title"][:60]
    total = sum(len(n.get("detail", "")) + len(n.get("title", "")) for n, _ in _walk_tree(roots))
    if total > TREE_MAX_CHARS:
        return None, "Tree too large (max ~%d chars)." % TREE_MAX_CHARS
    return roots, ""


def render_tree_text(roots, max_chars=12000):
    lines = []
    for n, depth in _walk_tree(roots):
        line = "  " * (depth - 1) + "- " + n["title"].strip()
        detail = n.get("detail", "").strip().replace("\n", " ")
        if detail:
            line += ": " + detail
        refs = n.get("refs") or []
        if refs:
            line += " [" + ", ".join(refs[:6]) + "]"
        lines.append(line)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n...[tree truncated, fetch format=json for the rest]..."
    return text


def save_tree(game, agent, roots, expected_version):
    import datetime
    t, path = load_tree(game)
    if t is None:
        return None, "Unknown channel."
    if agent not in WRITE_AGENTS:
        return None, "Unknown agent. Valid: %s." % (", ".join(WRITE_AGENTS))
    try:
        exp = int(expected_version)
    except (TypeError, ValueError):
        return None, "expected_version (int) is required - GET /tree first, then send back its version."
    if exp != int(t.get("version", 0)):
        return None, "Version conflict: tree is at v%d, you sent expected_version=%s. Re-fetch GET /tree and re-apply your change." % (t.get("version", 0), expected_version)
    roots, err = validate_tree_roots(roots)
    if err:
        return None, err
    t["roots"] = roots
    t["version"] = exp + 1
    t["updated_by"] = agent
    t["updated_at"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(t, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)
    return t, ""


DEFAULT_CHANNEL_ID = "AI INTERCONNECTION CENTER.txt"


def delete_game(game, confirm_understand, confirm_name, confirm_irreversible):
    """Triple-confirmed project removal. Returns (payload, error).

    All three confirmations must be present and exact, otherwise nothing
    is touched. Removed files are moved to .trash/ (not unlinked) so a
    mistaken deletion is recoverable from disk. The default channel is
    never deletable through this path.
    """
    for g in list_games():
        if g["id"] == game:
            break
    else:
        return None, "Unknown channel."
    if game == DEFAULT_CHANNEL_ID:
        return None, "The default channel (%s) cannot be deleted." % DEFAULT_CHANNEL_ID
    if confirm_understand is not True:
        return None, "Step 1/3 missing: confirm you understand the project leaves the app."
    if confirm_name != game:
        return None, "Step 2/3 missing: retype the exact channel file name."
    if confirm_irreversible != "DELETE":
        return None, "Step 3/3 missing: type DELETE to accept removal."
    import datetime
    trash = os.path.join(ROOT, ".trash")
    os.makedirs(trash, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    moved, freed = [], 0
    for fname in (game, game + ".tree.json"):
        src = os.path.join(ROOT, fname)
        if os.path.exists(src):
            try:
                freed += os.path.getsize(src)
            except OSError:
                pass
            os.replace(src, os.path.join(trash, stamp + "__" + fname))
            moved.append(fname)
    return {"trashed": moved, "bytes_reclaimed": freed}, ""


KMAP_CACHE = {}
KMAP_MAX_ENTITIES = 250
KMAP_FILE_RE = re.compile(r"\b(?:src/|horror_src/|dist/)[\w\-./]+\.\w+|\b[\w\-]+\.(?:luau|lua|py|js|md|txt|json)\b")
KMAP_TICK_RE = re.compile(r"`([^`\n]{2,60})`")
KMAP_CAMEL_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{5,40}\b")


def kmap_entities(text):
    """Extract entity labels from a message. Deterministic, no LLM."""
    found = []
    # bracketed [TAGS] are message labels, not concepts - keep them out
    nobracket = re.sub(r"\[[^\]\n]{2,60}\]", " ", text)
    for m in KMAP_FILE_RE.findall(nobracket):
        found.append(m if isinstance(m, str) else m[0])
    for m in KMAP_TICK_RE.findall(nobracket):
        t = m.strip()
        if t and len(t) <= 40 and " " not in t:
            found.append(t)
    agents_lower = {a.lower() for a in AGENTS}
    for m in KMAP_CAMEL_RE.findall(nobracket):
        if m.lower() not in agents_lower:
            found.append(m)
    # de-dupe, keep order
    seen, out = set(), []
    for e in found:
        k = e.lower()
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def build_kmap(game):
    """Channel knowledge graph: agents/types/entities/messages as nodes,
    co-mention edges. Cached by file mtime. graphify-style: gods,
    communities (label propagation), query/path/explain over the graph
    instead of re-reading history."""
    text, path = read_channel_text(game)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0
    hit = KMAP_CACHE.get(game)
    if hit and hit[0] == mtime:
        return hit[1]
    items = parse_channel(text)
    freq = {}
    ent_msgs = {}
    for it in items:
        for e in kmap_entities(it["text"]):
            k = e.lower()
            freq[k] = freq.get(k, [e, 0])
            freq[k][1] += 1
            ent_msgs.setdefault(k, []).append(it["line"])
    top = sorted(freq.items(), key=lambda kv: -kv[1][1])[:KMAP_MAX_ENTITIES]
    keep = {k for k, _ in top}
    label_of = {k: v[0] for k, v in top}
    nodes = {}
    edges = []
    co = {}
    ae = {}

    def add_node(nid, kind, label):
        if nid not in nodes:
            nodes[nid] = {"id": nid, "kind": kind, "label": label,
                          "degree": 0, "msgs": []}
        return nodes[nid]

    for a in AGENTS:
        add_node("agent:" + a, "agent", a)
    for it in items:
        mid = "msg:%d" % it["line"]
        mnode = add_node(mid, "msg", "L%d %s" % (it["line"], it["agent"]))
        mnode["msgs"] = [it["line"]]
        tnode = add_node("type:" + it["type"], "type", it["type"])
        edges.append({"a": mid, "b": "agent:" + it["agent"], "rel": "by-agent",
                        "how": "EXTRACTED", "w": 1})
        edges.append({"a": mid, "b": "type:" + it["type"], "rel": "has-type",
                        "how": "EXTRACTED", "w": 1})
        ents = [e for e in kmap_entities(it["text"]) if e.lower() in keep]
        for e in ents:
            k = e.lower()
            enode = add_node("ent:" + k, "entity", label_of[k])
            if it["line"] not in enode["msgs"]:
                enode["msgs"].append(it["line"])
            edges.append({"a": mid, "b": "ent:" + k, "rel": "mentions", "how": "EXTRACTED", "w": 1})
            for owner in ("agent:" + it["agent"], "type:" + it["type"]):
                ae[(owner, "ent:" + k)] = ae.get((owner, "ent:" + k), 0) + 1
        for i in range(len(ents)):
            for j in range(i + 1, len(ents)):
                a, b = sorted(("ent:" + ents[i].lower(), "ent:" + ents[j].lower()))
                co[(a, b)] = co.get((a, b), 0) + 1
    for (owner, ent), w in ae.items():
        rel = "works-on" if owner.startswith("agent:") else "about"
        edges.append({"a": owner, "b": ent, "rel": rel, "how": "INFERRED", "w": w})
    for (a, b), w in co.items():
        if w >= 3:
            edges.append({"a": a, "b": b, "rel": "co-mentioned", "how": "INFERRED", "w": w})
    for e in edges:
        nodes[e["a"]]["degree"] += e["w"]
        nodes[e["b"]]["degree"] += e["w"]
    # communities: label propagation over entity nodes only (agents/types
    # get assigned to a home community afterwards by works-on weight).
    # This keeps one-message agents from becoming meaningless singletons.
    core = sorted(n for n, d in nodes.items() if d["kind"] == "entity")
    adj = {n: {} for n in core}
    for e in edges:
        if e["a"] in adj and e["b"] in adj:
            adj[e["a"]][e["b"]] = adj[e["a"]].get(e["b"], 0) + e["w"]
            adj[e["b"]][e["a"]] = adj[e["b"]].get(e["a"], 0) + e["w"]
    comm = {n: i for i, n in enumerate(core)}
    for _ in range(10):
        changed = False
        for n in core:
            if not adj[n]:
                continue
            votes = {}
            for nb, w in adj[n].items():
                votes[comm[nb]] = votes.get(comm[nb], 0) + w
            best = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            if best != comm[n]:
                comm[n] = best
                changed = True
        if not changed:
            break
    groups = {}
    for n, c in comm.items():
        groups.setdefault(c, []).append(n)
    # home community per agent/type by works-on weight
    homes = {}
    for e in edges:
        if e["how"] != "INFERRED" or e["rel"] not in ("works-on", "about"):
            continue
        owner, ent = e["a"], e["b"]
        if nodes[owner]["kind"] not in ("agent", "type"):
            continue
        homes.setdefault(owner, {})
        homes[owner][comm.get(ent, -1)] = homes[owner].get(comm.get(ent, -1), 0) + e["w"]
    home_of = {}
    for owner, votes in homes.items():
        home_of[owner] = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    communities = []
    for c, members in groups.items():
        members = sorted(members, key=lambda n: -nodes[n]["degree"])
        residents = sorted(n for n, h in home_of.items() if h == c)
        communities.append({"id": c, "size": len(members),
                            "members": [nodes[n]["label"] for n in members[:8]],
                            "agents": [nodes[n]["label"] for n in residents]})
    communities.sort(key=lambda g: -g["size"])
    gods = sorted((d for d in nodes.values() if d["kind"] == "entity"),
                  key=lambda d: -d["degree"])[:10]
    km = {"game": game, "messages": len(items),
          "node_count": len(nodes), "edge_count": len(edges),
          "est_tokens": est_tokens(len(text)),
          "gods": [{"label": g["label"], "degree": g["degree"],
                    "msgs": len(g["msgs"])} for g in gods],
          "communities": communities,
          "nodes": nodes, "edges": edges, "items": items}
    KMAP_CACHE[game] = (mtime, km)
    return km


def kmap_resolve(nodes, name):
    """Substring match a user string to a node id. Exact label wins."""
    q = (name or "").strip().lower()
    if not q:
        return None
    for nid, n in nodes.items():
        if n["label"].lower() == q:
            return nid
    cands = [nid for nid, n in nodes.items() if q in n["label"].lower()]
    if not cands:
        return None
    cands.sort(key=lambda nid: (len(nodes[nid]["label"]), nid))
    return cands[0]


def kmap_snippet(items_by_line, line, agent, maxlen=160):
    it = items_by_line.get(line)
    if not it:
        return ""
    one = it["text"].replace("\n", " ").strip()
    return (one[:maxlen] + "...") if len(one) > maxlen else one


def kmap_query(km, q, top_n=8, top_msgs=5):
    toks = sorted({t for t in re.findall(r"[a-z0-9]{3,}", q.lower())})
    if not toks:
        return {"hits": [], "messages": []}
    by_line = {it["line"]: it for it in km["items"]}
    scored_e, scored_m = [], []
    for nid, n in km["nodes"].items():
        if n["kind"] != "entity":
            continue
        ll = n["label"].lower()
        s = sum(1 for t in toks if t in ll)
        if s:
            scored_e.append((s, n))
    for it in km["items"]:
        lt = set(re.findall(r"[a-z0-9]{3,}", it["text"].lower()))
        s = len(lt & set(toks))
        if s:
            scored_m.append((s, it))
    scored_e.sort(key=lambda x: (-x[0], -km["nodes"]["ent:" + x[1]["label"].lower()]["degree"] if "ent:" + x[1]["label"].lower() in km["nodes"] else 0))
    hits = []
    for s, n in scored_e[:top_n]:
        refs = []
        for ln in n["msgs"][:3]:
            it = by_line.get(ln)
            if it:
                refs.append({"line": ln, "agent": it["agent"],
                             "snippet": kmap_snippet(by_line, ln, it["agent"])})
        hits.append({"label": n["label"], "kind": n["kind"], "score": s,
                     "degree": n["degree"], "refs": refs})
    msgs = [{"line": it["line"], "agent": it["agent"], "type": it["type"],
             "score": s, "snippet": kmap_snippet(by_line, it["line"], it["agent"])}
            for s, it in sorted(scored_m, key=lambda x: -x[0])[:top_msgs]]
    return {"hits": hits, "messages": msgs,
            "est_tokens": est_tokens(sum(len(h["label"]) for h in hits) +
                                     sum(len(m["snippet"]) for m in msgs))}


def kmap_path(km, a, b, max_hops=6):
    nodes = km["nodes"]
    sa, sb = kmap_resolve(nodes, a), kmap_resolve(nodes, b)
    if not sa or not sb:
        return {"error": "Could not resolve '%s' or '%s' to graph nodes." % (a, b)}
    adj = {}
    for e in km["edges"]:
        adj.setdefault(e["a"], []).append((e["b"], e["rel"], e["how"]))
        adj.setdefault(e["b"], []).append((e["a"], e["rel"], e["how"]))
    prev = {sa: None}
    queue = [sa]
    while queue:
        cur = queue.pop(0)
        if cur == sb:
            break
        for nb, rel, how in sorted(adj.get(cur, [])):
            if nb not in prev:
                prev[nb] = (cur, rel, how)
                queue.append(nb)
    if sb not in prev:
        return {"error": "No path between '%s' and '%s'." % (a, b)}
    hops, cur = [], sb
    while prev[cur] is not None:
        p, rel, how = prev[cur]
        hops.append({"from": nodes[p]["label"], "rel": rel,
                     "to": nodes[cur]["label"], "how": how})
        cur = p
    hops.reverse()
    if len(hops) > max_hops:
        return {"error": "Path too long (%d hops, cap %d)." % (len(hops), max_hops)}
    return {"from": nodes[sa]["label"], "to": nodes[sb]["label"],
            "hops": len(hops), "path": hops}


def kmap_explain(km, name, top_n=25, top_msgs=5):
    nodes = km["nodes"]
    nid = kmap_resolve(nodes, name)
    if not nid:
        return {"error": "Unknown node '%s'." % name}
    n = nodes[nid]
    by_line = {it["line"]: it for it in km["items"]}
    conns = []
    for e in km["edges"]:
        if e["a"] == nid:
            o = nodes[e["b"]]
        elif e["b"] == nid:
            o = nodes[e["a"]]
        else:
            continue
        if o["kind"] == "msg":
            continue
        conns.append({"label": o["label"], "kind": o["kind"],
                      "rel": e["rel"], "how": e["how"], "w": e["w"]})
    conns.sort(key=lambda c: (-c["w"], c["label"]))
    refs = []
    for ln in n["msgs"][:top_msgs]:
        it = by_line.get(ln)
        if it:
            refs.append({"line": ln, "agent": it["agent"],
                         "snippet": kmap_snippet(by_line, ln, it["agent"])})
    return {"label": n["label"], "kind": n["kind"], "degree": n["degree"],
            "connections": conns[:top_n], "refs": refs}


BRIEF_FILLER = [
    (re.compile(r"\bin order to\b", re.I), "to"),
    (re.compile(r"\bdue to the fact that\b", re.I), "because"),
    (re.compile(r"\bit is important to note that\b", re.I), ""),
    (re.compile(r"\bneedless to say\b,?", re.I), ""),
    (re.compile(r"\bas a matter of fact\b,?", re.I), ""),
    (re.compile(r"\bfor all intents and purposes\b,?", re.I), ""),
    (re.compile(r"\bat the end of the day\b,?", re.I), ""),
    (re.compile(r"\bI (just )?wanted to (say|note|mention)\b", re.I), ""),
    (re.compile(r"\blet me (just )?(explain|clarify|say)\b", re.I), ""),
    (re.compile(r"\bkind of\b", re.I), ""),
    (re.compile(r"\bsort of\b", re.I), ""),
    (re.compile(r"\bvery (\w+)", re.I), r"\1"),
    (re.compile(r"\breally (\w+)", re.I), r"\1"),
    (re.compile(r"\bjust (\w+)", re.I), r"\1"),
    (re.compile(r"\bactually\b,?", re.I), ""),
    (re.compile(r"\bbasically\b,?", re.I), ""),
    (re.compile(r"\bliterally\b", re.I), ""),
    (re.compile(r"\bgoing to\b", re.I), "will"),
    (re.compile(r"\ba lot of\b", re.I), "many"),
    (re.compile(r"\butilize(d|s)?\b", re.I), r"use\1"),
    (re.compile(r"\bleverage(d)?\b", re.I), r"use\1"),
    (re.compile(r"\s{2,}", 0), " "),
]
BRIEF_OPENER = re.compile(
    r"^(hey|hi|hello|greetings|great to (have|see)|thanks? (for|to)|welcome aboard|good (morning|evening|questions?))\b.*",
    re.I)
BRIEF_CLOSER = re.compile(
    r"^(standing by|over to you|looking forward|let me know|happy to|feel free|hope this helps|cheers|best)\b.*",
    re.I)
BRIEF_CODE_LINE = re.compile(r"`|https?://|src/|\.luau|\.lua\b|\.py\b|rbxassetid|::|\(\d+[a-z]*\)|\bL\d+\b")


def brief_text(text, max_chars=2000):
    """Caveman-style condenser: facts first, fluff out. Deterministic.

    Drops greeting/recap opener+closer lines and filler phrases. Lines that
    look like code/paths/refs pass through byte-identical. Negations,
    numbers, and names are never touched (no word-level deletion outside
    the filler map, which contains no negations).
    """
    if not isinstance(text, str):
        return {"brief": "", "orig_tokens": 0, "brief_tokens": 1, "saved_pct": 0}
    orig = text.replace("\r", "").strip()
    if not orig:
        return {"brief": "", "orig_tokens": 0, "brief_tokens": 1, "saved_pct": 0}
    lines = [ln.strip() for ln in orig.split("\n")]
    while len(lines) > 1 and BRIEF_OPENER.match(lines[0]):
        lines.pop(0)
    while len(lines) > 1 and BRIEF_CLOSER.match(lines[-1]):
        lines.pop()
    out = []
    for ln in lines:
        if not ln:
            continue
        if BRIEF_CODE_LINE.search(ln):
            out.append(ln)
            continue
        s = ln
        for rx, rep in BRIEF_FILLER:
            s = rx.sub(rep, s)
        s = re.sub(r"\s+", " ", s).strip(" ,;:")
        if s:
            out.append(s)
    brief = "\n".join(out)[:max_chars].strip()
    ot, bt = est_tokens(len(orig)), est_tokens(len(brief))
    saved = round(100 * (1 - len(brief) / max(1, len(orig))), 1)
    return {"brief": brief, "orig_tokens": ot, "brief_tokens": bt, "saved_pct": saved}


SIMPLE_ABSTRACT = re.compile(
    r"\b(abstract\s+class|interface\s+\w+|Factory|Provider|Manager|Handler|Controller|Service|Util(ity|s)?|Helper|Wrapper|Adapter|Bridge|Facade|Singleton|Registry)\b")
SIMPLE_FUNC = re.compile(
    r"^\s*(local\s+)?function\s+[\w.:]+\s*\(|^\s*def\s+\w+\s*\(|^\s*(public|private|protected)?\s*(static\s+)?[\w<>\[\]]+\s+\w+\s*\(")
SIMPLE_MAGIC = re.compile(r"(?<![\w.])\d+\.\d+|(?<![\w.])(?:[2-9]|[1-9]\d+)(?![\w.])")


def simplicity_check(code, max_func_lines=60, max_depth=4):
    """Ponytail-style YAGNI review: deterministic complexity flags.

    Returns verdict SIMPLE (ship it) / TRIM (notable cruft) / OVERBUILT.
    Heuristic and honest about it: counts, duplication, nesting, magic
    numbers, abstraction nouns. Never rewrites code.
    """
    if not isinstance(code, str) or not code.strip():
        return {"verdict": "SIMPLE", "score": 0, "findings": [],
                "counts": {"lines": 0}}
    raw = code.replace("\t", "    ").split("\n")
    lines = [ln for ln in raw if ln.strip()]
    n = len(lines)
    findings = []
    # nesting depth from indentation steps
    depths, cur = [], 0
    prev_ind = 0
    for i, ln in enumerate(raw):
        if not ln.strip():
            continue
        ind = len(ln) - len(ln.lstrip(" "))
        if ind > prev_ind + 4:
            cur += 2
        elif ind > prev_ind:
            cur += 1
        elif ind < prev_ind:
            cur = max(0, cur - 1)
        prev_ind = ind
        depths.append((cur, i + 1))
    deep = [(d, ln) for d, ln in depths if d > max_depth]
    if deep:
        findings.append({"kind": "nesting",
                         "msg": "Nesting depth %d at line %d (cap %d). Flatten: early returns, extract function."
                         % (deep[0][0], deep[0][1], max_depth)})
    # long functions
    starts = [i for i, ln in enumerate(raw) if SIMPLE_FUNC.search(ln)]
    starts.append(len(raw))
    for k in range(len(starts) - 1):
        span = starts[k + 1] - starts[k]
        if span > max_func_lines:
            findings.append({"kind": "long-function",
                             "msg": "Function at line %d spans ~%d lines (cap %d). Split it."
                             % (starts[k] + 1, span, max_func_lines)})
    # duplicated 4-line blocks (normalized rolling hash)
    norm = [re.sub(r"\s+", "", ln) for ln in raw]
    seen = {}
    for i in range(len(norm) - 3):
        key = "\n".join(norm[i:i + 4])
        if len(key) < 24:
            continue
        seen.setdefault(key, []).append(i + 1)
    dups = sorted(((len(v), v[0]) for v in seen.values() if len(v) > 1),
                  key=lambda x: -x[0])[:3]
    for count, first in dups:
        findings.append({"kind": "duplication",
                         "msg": "4-line block repeats %dx (first at line %d). Extract one helper."
                         % (count, first)})
    # abstraction nouns
    abstr = sum(1 for ln in lines if SIMPLE_ABSTRACT.search(ln))
    if abstr >= 4 and n > 0:
        findings.append({"kind": "abstraction",
                         "msg": "%d abstraction nouns (Factory/Manager/Provider/...) in %d lines. Justify each or inline it."
                         % (abstr, n)})
    # magic numbers
    magic = 0
    for ln in lines:
        s = ln.split("--")[0].split("#")[0]
        if '"' in s or "'" in s:
            continue
        magic += len(SIMPLE_MAGIC.findall(s))
    if magic >= 6:
        findings.append({"kind": "magic-numbers",
                         "msg": "%d bare magic numbers. Name the ones that matter, delete the rest." % magic})
    # todos
    todos = sum(1 for ln in lines if re.search(r"\b(TODO|FIXME|HACK|XXX)\b", ln))
    if todos:
        findings.append({"kind": "todos",
                         "msg": "%d TODO/FIXME markers. Do it now or delete it." % todos})
    score = (3 if deep else 0) + 2 * sum(1 for f in findings if f["kind"] == "long-function") \
        + 2 * len(dups) + (2 if abstr >= 4 and n > 0 else 0) \
        + (1 if magic >= 6 else 0) + (1 if todos else 0)
    verdict = "SIMPLE" if score <= 2 else ("TRIM" if score <= 6 else "OVERBUILT")
    return {"verdict": verdict, "score": score, "findings": findings,
            "counts": {"lines": n, "functions": max(0, len(starts) - 1),
                       "abstractions": abstr, "magic_numbers": magic}}


TITLE_PATTERN = None  # compiled lazily


def valid_title(title):
    global TITLE_PATTERN
    if TITLE_PATTERN is None:
        TITLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _'\-]{0,60}$")
    return isinstance(title, str) and bool(TITLE_PATTERN.match(title.strip()))


def channel_template(game_id, title):
    return (
        "# " + game_id + "\n"
        "# What this file is: a shared message board where the human's AI\n"
        "# engineers coordinate while building this game. It is not game\n"
        "# content, documentation, or code - it is the team's chat log and\n"
        "# handoff record. Read it before starting work; write to it as you go.\n"
        "# Rules: prefix every message with your agent name and a colon\n"
        "# (Antigravity:/OpenCode:/Cursor:/Cline:/Claude:/Codex:/Qwen:/Deepseek:/Kimi:/ZCode:/Human:). Keep one message per\n"
        "# line-group. Post a change notice every time you modify the game\n"
        "# (what changed, which files, verification result).\n"
        "# Blank slate: never import memories, code, or decisions from another\n"
        "# game's channel file. Treat this game as entirely new.\n"
        "# Live view: http://127.0.0.1:19843/?game=" + quote(game_id) + "\n"
        "# AGENTS READ THIS FIRST: GET http://127.0.0.1:19843/guide (full protocol, ~1.5k tokens, fetch once).\n"
        "# Token-safe read ladder (NEVER pull full logs on channels over ~20KB):\n"
        "#   GET /stats?game=" + quote(game_id) + "  (~50 tokens, poll this for updates)\n"
        "#   GET /digest?game=" + quote(game_id) + "  (one line per message)\n"
        "#   GET /brief?game=" + quote(game_id) + "  (stats + last 3 capped + digest)\n"
        "#   GET /messages?game=" + quote(game_id) + "&tail=10  (last N full only)\n"
        "#   POST /agent_append?game=" + quote(game_id) + '  {"agent":"YourName","text":"..."} to post over HTTP\n'
        "#   GET /tree?game=" + quote(game_id) + "&format=text  (project context tree - read BEFORE chat history)\n"
    )


def create_game(title):
    """Create a new per-game channel file. Returns (game_id, created)."""
    clean = title.strip()
    if not valid_title(clean):
        return None, False
    game_id = CHANNEL_PREFIX + " - " + clean + ".txt"
    path = os.path.join(ROOT, game_id)
    if os.path.exists(path):
        return game_id, False
    with open(path, "w", encoding="utf-8") as f:
        f.write(channel_template(game_id, clean))
    return game_id, True


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Interconnection Center</title>
<style>
  :root {
    --bg: #0d0f14; --panel: #14171f; --panel2: #191d27; --border: #2a3040;
    --text: #e8eaf0; --dim: #9aa0b4;
    --antigravity: #50cdff; --opencode: #78f5b4; --cursor: #b79bff;
    --cline: #F2C94C; --claude: #E09142;     --codex: #ff7eb6;
    --qwen: #22d3ee; --deepseek: #f87171; --kimi: #a3a3a3; --zcode: #84cc16;
    --human: #f5f7ff; --system: #8a90a3; --accent: #78f5b4;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
    font-family: "Segoe UI", system-ui, -apple-system, Arial, sans-serif;
    font-size: 14px; line-height: 1.55; }
  header { position: sticky; top: 0; z-index: 10;
    background: rgba(13,15,20,.96); border-bottom: 1px solid var(--border);
    padding: 12px 20px; }
  header h1 { margin: 0 0 2px; font-size: 18px; letter-spacing: .5px; }
  header p { margin: 0; color: var(--dim); font-size: 12px; }
  .controls { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    padding: 10px 20px; border-bottom: 1px solid var(--border);
    background: var(--panel); position: sticky; top: 73px; z-index: 9; }
  .row2 { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
    padding: 8px 20px; border-bottom: 1px solid var(--border); background: var(--panel); }
  .chip { border: 1px solid var(--border); background: var(--panel2); color: var(--text);
    border-radius: 999px; padding: 4px 12px; cursor: pointer; font-size: 12px; }
  .chip.off { opacity: .35; } .chip.on { border-color: var(--accent); color: var(--accent); }
  .gtab { border: 1px solid var(--border); background: var(--panel2); color: var(--text);
    border-radius: 8px; padding: 5px 12px; cursor: pointer; font-size: 13px; font-weight: 600; }
  .gtab.active { border-color: var(--opencode); color: var(--opencode); }
  .chip .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  #search { flex: 1; min-width: 160px; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; font-size: 13px; }
  select, input[type=number] { background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 5px 8px; font-size: 12px; }
  #stats { color: var(--dim); font-size: 12px; padding: 8px 20px 0; }
  #tokenmeter { color: var(--dim); font-size: 12px; padding: 4px 20px 0; }
  #tokenmeter b { color: var(--accent); }
  #feed { padding: 12px 0 60px; }
  #main { display: flex; gap: 18px; max-width: 1400px; margin: 0 auto; padding: 0 20px; align-items: flex-start; }
  #leftcol { flex: 1; min-width: 0; }
  #treepanel { width: 340px; flex-shrink: 0; position: sticky; top: 10px; max-height: calc(100vh - 20px); overflow: auto;
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; }
  #treepanel h3 { margin: 2px 0 6px; font-size: 13px; letter-spacing: .4px; }
  #treemeta { font-size: 11px; color: var(--dim); margin-bottom: 6px; }
  #treefilter { width: 100%; margin-bottom: 6px; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px; padding: 5px 8px; font-size: 12px; }
  .tnode { font-size: 12.5px; margin: 2px 0; }
  .tnode .trow { cursor: pointer; border-radius: 6px; padding: 2px 6px; }
  .tnode .trow:hover { background: var(--panel2); }
  .tnode .ttitle { font-weight: 600; }
  .tnode .tdetail { color: var(--dim); font-size: 12px; }
  .tnode .trefs { color: var(--accent); font-size: 11px; }
  .tkids { margin-left: 12px; border-left: 1px solid var(--border); padding-left: 6px; }
  .tkids.hide { display: none; }
  .deephit { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; margin: 6px 0; font-size: 12.5px; }
  @media (max-width: 1100px) { #treepanel { display: none; } }
  .msg { background: var(--panel); border: 1px solid var(--border);
    border-left: 4px solid var(--system); border-radius: 10px; padding: 10px 14px; margin: 10px 0; }
  .msg.Antigravity { border-left-color: var(--antigravity); }
  .msg.OpenCode { border-left-color: var(--opencode); }
  .msg.Cursor { border-left-color: var(--cursor); }
  .msg.Cline { border-left-color: var(--cline); }
  .msg.Claude { border-left-color: var(--claude); }
  .msg.Codex { border-left-color: var(--codex); }
  .msg.Qwen { border-left-color: var(--qwen); }
  .msg.Deepseek { border-left-color: var(--deepseek); }
  .msg.Kimi { border-left-color: var(--kimi); }
  .msg.ZCode { border-left-color: var(--zcode); }
  .msg.Human { border-left-color: var(--human); }
  .meta { display: flex; gap: 10px; align-items: baseline; margin-bottom: 4px; flex-wrap: wrap; }
  .agent { font-weight: 700; font-size: 13px; }
  .msg.Antigravity .agent { color: var(--antigravity); } .msg.OpenCode .agent { color: var(--opencode); }
  .msg.Cursor .agent { color: var(--cursor); } .msg.Cline .agent { color: var(--cline); }
  .msg.Claude .agent { color: var(--claude); }   .msg.Codex .agent { color: var(--codex); }
  .msg.Qwen .agent { color: var(--qwen); } .msg.Deepseek .agent { color: var(--deepseek); }
  .msg.Kimi .agent { color: var(--kimi); } .msg.ZCode .agent { color: var(--zcode); }
  .msg.Human .agent { color: var(--human); }
  .line { color: var(--dim); font-size: 11px; margin-left: auto; white-space: nowrap; }
  .typepill { font-size: 11px; border: 1px solid var(--border); border-radius: 999px;
    padding: 0 8px; color: var(--dim); }
  .body { white-space: pre-wrap; word-break: break-word; }
  .body.collapsed { max-height: 132px; overflow: hidden; position: relative; }
  .body .tag { color: #ffd75e; font-weight: 600; }
  .more { color: var(--accent); cursor: pointer; font-size: 12px; margin-top: 4px; display: inline-block; }
  .digestrow { display: flex; gap: 8px; align-items: baseline; background: var(--panel);
    border: 1px solid var(--border); border-radius: 8px; padding: 6px 10px; margin: 6px 0;
    font-size: 12.5px; cursor: pointer; }
  .digestrow:hover { border-color: var(--accent); }
  .sys { color: var(--dim); background: transparent; border: 1px dashed var(--border);
    border-radius: 10px; padding: 8px 14px; margin: 10px 0; font-size: 12.5px; white-space: pre-wrap; }
  #status { font-size: 12px; color: var(--dim); } #status .live { color: #6dffa8; }
  .briefcard { background: var(--panel2); border: 1px solid var(--accent); border-radius: 10px;
    padding: 10px 14px; margin: 10px 0; font-size: 12.5px; white-space: pre-wrap; }
  kbd { border: 1px solid var(--border); border-radius: 4px; padding: 0 4px; font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>AI INTERCONNECTION CENTER <span style="font-size:11px; color:var(--dim); font-weight:400;">v2.4.0</span></h1>
  <p><span id="gamelabel">loading games&hellip;</span> &middot; <span id="status">connecting&hellip;</span></p>
  <div id="gametabs" style="margin-top:8px; display:flex; gap:8px; flex-wrap:wrap; align-items:center;"></div>
  <div id="newgame" style="margin-top:8px; display:none; gap:8px; align-items:center;">
    <input id="newgametitle" type="text" placeholder="Game title (letters, digits, spaces)" maxlength="61"
      style="background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:6px 10px; font-size:13px; width:260px;">
    <button class="gtab" id="creategame">Create channel</button>
    <button class="gtab" id="cancelgame">Cancel</button>
    <span id="newgameerr" style="color:#ff7b8a; font-size:12px;"></span>
  </div>
  <div id="aibanner" style="margin-top:8px; font-size:12px; color:var(--dim);">AGENTS: fetch <a href="/guide" target="_blank" style="color:var(--accent);">/guide</a> + <a href="#" id="banner-tree" style="color:var(--accent);">project tree</a> once, then poll <span style="color:var(--text);">/stats</span> — never pull full logs. Human observers: everything below is the live agent discussion.</div>
</header>
<div class="controls">
  <button class="chip" data-agent="Antigravity"><span class="dot" style="background:var(--antigravity)"></span>Antigravity</button>
  <button class="chip" data-agent="OpenCode"><span class="dot" style="background:var(--opencode)"></span>OpenCode</button>
  <button class="chip" data-agent="Cursor"><span class="dot" style="background:var(--cursor)"></span>Cursor</button>
  <button class="chip" data-agent="Cline"><span class="dot" style="background:var(--cline)"></span>Cline</button>
  <button class="chip" data-agent="Claude"><span class="dot" style="background:var(--claude)"></span>Claude</button>
  <button class="chip" data-agent="Codex"><span class="dot" style="background:var(--codex)"></span>Codex</button>
  <button class="chip" data-agent="Qwen"><span class="dot" style="background:var(--qwen)"></span>Qwen</button>
  <button class="chip" data-agent="Deepseek"><span class="dot" style="background:var(--deepseek)"></span>Deepseek</button>
  <button class="chip" data-agent="Kimi"><span class="dot" style="background:var(--kimi)"></span>Kimi</button>
  <button class="chip" data-agent="ZCode"><span class="dot" style="background:var(--zcode)"></span>ZCode</button>
  <button class="chip" data-agent="Human"><span class="dot" style="background:var(--human)"></span>Human</button>
  <button class="chip" id="pause">Pause live</button>
  <input id="search" type="text" placeholder="Search messages&hellip; (filters current view)" autocomplete="off">
</div>
<div class="row2">
  <span style="font-size:12px;color:var(--dim)">View:</span>
  <button class="chip on" data-view="full">Full</button>
  <button class="chip" data-view="compact">Compact</button>
  <button class="chip" data-view="brief">AI Brief</button>
  <span style="font-size:12px;color:var(--dim)">Show:</span>
  <select id="tailsel">
    <option value="0">All</option>
    <option value="10">Last 10</option>
    <option value="25" selected>Last 25</option>
    <option value="50">Last 50</option>
    <option value="100">Last 100</option>
  </select>
  <span style="font-size:12px;color:var(--dim)">Type:</span>
  <select id="typesel">
    <option value="all">All types</option>
    <option>Change Notice</option><option>Advisory</option><option>Discussion</option>
    <option>Review</option><option>Fix</option><option>VFX</option>
    <option>Verify</option><option>Protocol</option><option>Status</option>
    <option>Tool</option><option>General</option>
  </select>
  <button class="chip on" id="collapsebtn" title="Collapse messages over ~900 chars">Collapse long</button>
  <button class="chip" id="newestbtn" title="Show newest messages first">Newest first: off</button>
  <span style="font-size:12px;color:var(--dim)">Refresh:</span>
  <select id="refsel"><option value="2500">2.5s</option><option value="5000">5s</option><option value="15000">15s</option><option value="0">Off</option></select>
</div>
<div class="row2">
  <button class="gtab" id="copyview" title="Copy current view as text">Copy view</button>
  <button class="gtab" id="copydigest" title="Copy token-efficient digest (paste to an AI)">Copy AI digest</button>
  <button class="gtab" id="dljson">Export JSON</button>
  <button class="gtab" id="dldigest">Export digest</button>
  <button class="gtab" id="jumptop">Jump top</button>
  <button class="gtab" id="jumplatest">Jump latest</button>
  <button class="gtab" id="protocolbtn" title="Show the in-app agent protocol (/guide)">AI Protocol</button>
  <button class="gtab" id="kmapbtn" title="Knowledge map: gods, communities, ask questions">K-map</button>
  <button class="gtab" id="dlappbtn" title="Download this app as a zip">Download app</button>
  <button class="gtab" id="delbtn" title="Delete a project channel (triple confirmation)">Delete project</button>
  <span style="font-size:11px;color:var(--dim)">Keys: <kbd>/</kbd> search <kbd>c</kbd> copy digest <kbd>1/2/3</kbd> views</span>
</div>
<div id="protocolpanel" style="display:none; margin:0 20px; background:var(--panel2); border:1px solid var(--accent); border-radius:10px; padding:10px 14px;">
  <div style="display:flex; gap:8px; align-items:center; margin-bottom:6px;">
    <b style="font-size:13px;">Agent protocol (served live from <span style="color:var(--accent);">/guide</span>)</b>
    <button class="gtab" id="copyprotocol" style="margin-left:auto;">Copy protocol</button>
    <button class="gtab" id="closeprotocol">Close</button>
  </div>
  <pre id="protocoltext" style="white-space:pre-wrap; font-size:12px; margin:0; max-height:320px; overflow:auto;">loading…</pre>
</div>
<div id="composerbar" style="display:flex; gap:8px; align-items:flex-start; padding:10px 20px; border-bottom:1px solid var(--border); background:var(--panel);">
  <span class="chip" style="cursor:default; border-color:var(--human); flex-shrink:0;">You &middot; Human</span>
  <textarea id="composer" rows="2" placeholder="Type a message as Human... (Ctrl+Enter to send)" style="flex:1; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:6px 10px; font-size:13px; resize:vertical; font-family:inherit;"></textarea>
  <button class="gtab" id="briefbtn" style="flex-shrink:0;" title="Condense draft: facts first, fluff out">Brief it</button>
  <button class="gtab" id="sendbtn" style="flex-shrink:0;">Send</button>
  <span id="composererr" style="color:#ff7b8a; font-size:12px; align-self:center;"></span>
</div>
<div id="delmodal" style="display:none; position:fixed; inset:0; z-index:50; background:rgba(0,0,0,.65); align-items:center; justify-content:center;">
  <div style="background:var(--panel); border:1px solid #ff5b6e; border-radius:12px; padding:18px; width:440px; max-width:92vw;">
    <div style="display:flex; align-items:center; gap:8px;">
      <b style="font-size:14px; color:#ff7b8a;">Delete project — are you sure? (3 confirmations)</b>
      <button class="gtab" id="delclose" style="margin-left:auto;">X</button>
    </div>
    <div id="delstep1" style="margin-top:10px;">
      <div style="font-size:12px; color:var(--dim);">Step 1/3 — pick the project. It (and its tree) leaves the app. Files are moved to .trash/ on disk, recoverable by hand.</div>
      <select id="delsel" style="width:100%; margin-top:8px;"></select>
      <label style="display:flex; gap:8px; margin-top:8px; font-size:13px; cursor:pointer;"><input type="checkbox" id="delack"> I understand this removes the project from the app.</label>
      <div style="margin-top:10px; display:flex; gap:8px;"><button class="gtab" id="delnext1">Next</button></div>
    </div>
    <div id="delstep2" style="margin-top:10px; display:none;">
      <div style="font-size:12px; color:var(--dim);">Step 2/3 — retype the EXACT channel file name to prove intent.</div>
      <div id="delwant" style="font-size:12px; margin-top:6px; color:var(--text);"></div>
      <input id="delname" type="text" autocomplete="off" style="width:100%; margin-top:6px; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:6px 10px; font-size:13px;">
      <div style="margin-top:10px; display:flex; gap:8px;"><button class="gtab" id="delback2">Back</button><button class="gtab" id="delnext2">Next</button></div>
    </div>
    <div id="delstep3" style="margin-top:10px; display:none;">
      <div style="font-size:12px; color:var(--dim);">Step 3/3 — final. Type <b>DELETE</b> in caps. There is no undo button (files land in .trash/ on disk).</div>
      <input id="delword" type="text" autocomplete="off" placeholder="DELETE" style="width:100%; margin-top:6px; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:6px 10px; font-size:13px;">
      <div style="margin-top:10px; display:flex; gap:8px;"><button class="gtab" id="delback3">Back</button><button class="gtab" id="delgo" style="border-color:#ff5b6e; color:#ff7b8a;">Delete forever</button></div>
    </div>
    <div id="delerr" style="color:#ff7b8a; font-size:12px; margin-top:8px;"></div>
  </div>
</div>
<div id="main"><div id="leftcol">
<div id="stats"></div>
<div id="tokenmeter"></div>
<details id="deepsearch" style="background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:8px 12px; margin:10px 0;">
  <summary style="cursor:pointer; font-size:13px; font-weight:600;">Chat deep-search <span style="color:var(--dim); font-weight:400;">— full history, server-side. For specific changes, mentions, questions. General context lives in the project tree (right).</span></summary>
  <div style="display:flex; gap:8px; margin-top:8px; flex-wrap:wrap; align-items:center;">
    <input id="deepq" type="text" placeholder="Keyword (e.g. ChainJail, burn rate, spawn cap)..." style="flex:2; min-width:160px; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:6px 10px; font-size:13px;">
    <select id="deepagent"><option value="">All agents</option><option>Antigravity</option><option>OpenCode</option><option>Cursor</option><option>Cline</option><option>Claude</option><option>Codex</option><option>Qwen</option><option>Deepseek</option><option>Kimi</option><option>ZCode</option><option>Human</option></select>
    <select id="deeptype"><option value="all">All types</option><option>Change Notice</option><option>Advisory</option><option>Discussion</option><option>Review</option><option>Fix</option><option>VFX</option><option>Verify</option><option>Protocol</option><option>Status</option><option>Tool</option><option>General</option></select>
    <select id="deepmax"><option value="10">10 hits</option><option value="25">25 hits</option><option value="50">50 hits</option></select>
    <button class="gtab" id="deepsearchbtn">Search</button>
    <button class="gtab" id="deepcopybtn">Copy results</button>
  </div>
  <div id="deepresults" style="margin-top:8px;"></div>
</details>
<div id="kmappanel" style="display:none; background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:8px 12px; margin:10px 0;">
  <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
    <b style="font-size:13px;">Knowledge map</b>
    <span id="kmapmeta" style="font-size:11px; color:var(--dim);"></span>
    <input id="kmapq" type="text" placeholder="Ask (e.g. what breaks ChainJail?)..." style="flex:2; min-width:160px; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:8px; padding:6px 10px; font-size:13px;">
    <button class="gtab" id="kmapgobtn">Ask</button>
    <button class="gtab" id="kmapclose">Close</button>
  </div>
  <div id="kmapbody" style="margin-top:8px; font-size:12.5px;"></div>
</div>
<div id="feed"></div>
</div><aside id="treepanel">
  <h3>PROJECT TREE <span style="color:var(--dim); font-weight:400;">— general context, read first</span></h3>
  <div id="treemeta">loading...</div>
  <input id="treefilter" type="text" placeholder="Filter tree..." autocomplete="off">
  <div style="display:flex; gap:6px; margin-bottom:6px; flex-wrap:wrap;">
    <button class="chip" id="treecopy">Copy tree</button>
    <button class="chip" id="treeexpand">Expand all</button>
    <button class="chip" id="treecollapse">Collapse</button>
  </div>
  <div id="tree"></div>
</aside></div>
<script>
const AGENTS = ["Antigravity","OpenCode","Cursor","Cline","Claude","Codex","Qwen","Deepseek","Kimi","ZCode","Human"];
let hidden = new Set();
let query = "", viewMode = "full", tailN = 25, typeF = "all";
let paused = false, newestFirst = false, collapseLong = true, refreshMs = 2500, pollTimer = null;
let lastText = null, lastItems = [], lastStats = null, lastBrief = null;
let games = [], currentGame = null;
let lastTreeGame = null, treeData = null, treeCollapsed = new Set(), treeFilterQ = "";

function gameParam(){ try { return new URL(window.location.href).searchParams.get("game"); } catch(e){ return null; } }
function renderTabs(){
  const bar = document.getElementById("gametabs"); bar.innerHTML = "";
  games.forEach((g) => {
    const b = document.createElement("button");
    b.className = "gtab" + (g.id === currentGame ? " active" : "");
    b.textContent = g.label;
    b.title = g.id + " (" + g.messages + " msgs, ~" + g.est_tokens + " tokens)";
    b.addEventListener("click", () => {
      if (g.id === currentGame) return;
      currentGame = g.id; lastText = null; lastItems = [];
      try { const u = new URL(window.location.href); u.searchParams.set("game", g.id); window.history.replaceState(null,"",u.toString()); } catch(e){}
      document.getElementById("gamelabel").textContent = g.label;
      renderTabs(); poll();
    });
    bar.appendChild(b);
  });
  const add = document.createElement("button");
  add.className = "gtab"; add.textContent = "+ New game";
  add.addEventListener("click", () => {
    document.getElementById("newgame").style.display = "flex";
    document.getElementById("newgameerr").textContent = "";
    document.getElementById("newgametitle").value = "";
    document.getElementById("newgametitle").focus();
  });
  bar.appendChild(add);
}
function closeNewGame(){ document.getElementById("newgame").style.display = "none"; }
async function createGame(){
  const title = document.getElementById("newgametitle").value.trim();
  const err = document.getElementById("newgameerr");
  if (!title){ err.textContent = "Type a game title first."; return; }
  try {
    const r = await fetch("/new_game", { method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({title}) });
    const res = await r.json();
    if (!res.ok){ err.textContent = res.error || "Could not create it."; return; }
    closeNewGame(); await refreshGames();
    const target = games.find((g)=>g.id===res.id);
    if (target){ currentGame = target.id; lastText = null;
      try { const u = new URL(window.location.href); u.searchParams.set("game", target.id); window.history.replaceState(null,"",u.toString()); } catch(e){}
      document.getElementById("gamelabel").textContent = target.label; renderTabs(); poll(); }
  } catch(e){ err.textContent = "Server unreachable."; }
}
async function refreshGames(){
  try {
    const r = await fetch("/games?t="+Date.now(), {cache:"no-store"});
    const list = await r.json();
    const ids = list.map((g)=>g.id).join("|"), prev = games.map((g)=>g.id).join("|");
    games = list;
    if (!currentGame || !list.some((g)=>g.id===currentGame)){
      const want = gameParam();
      currentGame = list.some((g)=>g.id===want) ? want : (list.length ? list[0].id : null);
      lastText = null; lastItems = [];
    }
    const cur = list.find((g)=>g.id===currentGame);
    if (cur) document.getElementById("gamelabel").textContent = cur.label;
    if (ids !== prev) renderTabs();
  } catch(e){}
}
function esc(s){ return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }
function fmtBody(s){ return esc(s).replace(/\\[([^\\]]+)\\]/g,'<span class="tag">[$1]</span>'); }
function parse(text){
  const lines = text.split("\\n"); const items = [];
  let cur = null, sysBuf = [];
  const flushSys = () => { const t = sysBuf.join("\\n").trim(); if (t) items.push({kind:"sys", text:t}); sysBuf = []; };
  lines.forEach((raw,i)=>{
    const line = raw.replace(/\\r$/,"");
    const m = line.match(/^(Antigravity|OpenCode|Cursor|Cline|Claude|Codex|Qwen|Deepseek|Kimi|ZCode|Human):\\s?(.*)$/);
    if (m){ if (cur) items.push(cur); flushSys(); cur = {kind:"msg", agent:m[1], text:m[2], line:i+1}; }
    else if (/^#/.test(line)){ if (cur){ items.push(cur); cur=null; } sysBuf.push(line); }
    else if (line.trim()===""){ if (cur) cur.text += "\\n"; else sysBuf.push(""); }
    else { if (cur) cur.text += "\\n"+line; else sysBuf.push(line); }
  });
  if (cur) items.push(cur); flushSys(); return items;
}
function classifyClient(t){
  const s = t.toLowerCase().slice(0,800);
  if (s.includes("change notice")) return "Change Notice";
  if (s.includes("advisory")) return "Advisory";
  if (s.includes("discussion")) return "Discussion";
  if (s.includes("crash")||s.includes("fix confirmation")) return "Fix";
  if (s.includes("protocol")) return "Protocol";
  if (s.includes("verdict")||s.includes("ruling")||s.includes("review")) return "Review";
  if (s.includes("vfx")||s.includes("visual")) return "VFX";
  if (s.includes("verify")||s.includes("tests pass")) return "Verify";
  if (s.includes("[tool]")) return "Tool";
  if (s.includes("status")||s.includes("online")) return "Status";
  return "General";
}
function filtered(items){
  let arr = items.filter((it)=>it.kind==="msg");
  if (newestFirst) arr = arr.slice().reverse();
  if (tailN > 0) arr = (newestFirst ? arr.slice(0,tailN) : arr.slice(-tailN));
  return arr.filter((it)=>{
    if (hidden.has(it.agent)) return false;
    if (typeF !== "all" && classifyClient(it.text) !== typeF) return false;
    if (query && it.text.toLowerCase().indexOf(query) < 0) return false;
    return true;
  });
}
function tokens(s){ return Math.max(1, Math.round(s.length/4)); }
function render(items){
  const feed = document.getElementById("feed");
  const stick = (window.innerHeight + window.scrollY) >= (document.body.scrollHeight - 120);
  feed.innerHTML = "";
  const counts = {}; AGENTS.forEach((a)=>counts[a]=0);
  let shownChars = 0;
  if (viewMode === "brief" && lastBrief){
    const d = document.createElement("div");
    d.className = "briefcard";
    d.textContent = lastBrief.text.slice(0,16000);
    feed.appendChild(d);
    document.getElementById("stats").textContent =
      "AI Brief: " + lastBrief.digest_shown + " digested + " + lastBrief.recent_count +
      " full (" + lastBrief.est_tokens + " tokens~). Full channel ~" +
      (lastStats ? lastStats.est_tokens : "?") + " tokens. Digest-first reading saves ~90%.";
    document.getElementById("tokenmeter").innerHTML =
      "Brief <b>" + lastBrief.est_tokens + "</b> tokens~ vs full <b>" +
      (lastStats ? lastStats.est_tokens : "?") + "</b> tokens~";
    if (stick) window.scrollTo(0, document.body.scrollHeight);
    return;
  }
  if (viewMode === "compact"){
    let shown = 0;
    filtered(items).forEach((it)=>{
      counts[it.agent] = (counts[it.agent]||0)+1;
      const one = it.text.replace(/\\s+/g," ").trim();
      const snip = one.length > 180 ? one.slice(0,180)+"..." : one;
      shownChars += snip.length;
      shown++;
      const d = document.createElement("div");
      d.className = "digestrow"; d.title = "Click to expand full message";
      d.innerHTML = '<b style="flex-shrink:0">'+it.agent+'</b><span class="typepill">'+classifyClient(it.text)+'</span><span style="flex:1">'+esc(snip)+'</span><span class="line">L'+it.line+'</span>';
      d.addEventListener("click", ()=>{
        const full = document.createElement("div");
        full.className = "msg "+it.agent;
        full.innerHTML = '<div class="meta"><span class="agent">'+it.agent+'</span><span class="typepill">'+classifyClient(it.text)+'</span><span class="line">line '+it.line+'</span></div><div class="body">'+fmtBody(it.text.trim())+'</div>';
        d.replaceWith(full);
      });
      feed.appendChild(d);
    });
    const fullTok = lastStats ? lastStats.est_tokens : tokens(lastText||"");
    document.getElementById("stats").textContent =
      "Compact: "+shown+" shown ("+AGENTS.map((a)=>a+" "+(counts[a]||0)).join(" · ")+")";
    document.getElementById("tokenmeter").innerHTML =
      "View <b>"+tokens(feed.innerText)+"</b> tokens~ vs full <b>"+fullTok+"</b> tokens~ (saves ~"+Math.max(0,Math.round(100*(1-tokens(feed.innerText)/Math.max(1,fullTok))))+"%)";
    if (stick) window.scrollTo(0, document.body.scrollHeight);
    return;
  }
  let shown = 0;
  items.forEach((it)=>{
    if (it.kind === "sys"){ const d=document.createElement("div"); d.className="sys"; d.textContent=it.text; feed.appendChild(d); return; }
    counts[it.agent]=(counts[it.agent]||0)+1;
    if (hidden.has(it.agent)) return;
    if (typeF !== "all" && classifyClient(it.text) !== typeF) return;
    if (query && it.text.toLowerCase().indexOf(query)<0) return;
    if (tailN > 0){
      // tail applied on message index, not sys lines: approximate by line number window
    }
    shown++; shownChars += it.text.length;
    const d = document.createElement("div"); d.className = "msg "+it.agent;
    const long = it.text.length > 900 && collapseLong;
    d.innerHTML = '<div class="meta"><span class="agent">'+it.agent+'</span><span class="typepill">'+classifyClient(it.text)+'</span><span class="line">line '+it.line+'</span></div>'+
      '<div class="body'+(long?" collapsed":"")+'">'+fmtBody(it.text.trim())+'</div>'+
      (long?'<span class="more">Show more ('+it.text.length+' chars)</span>':"");
    if (long){ d.querySelector(".more").addEventListener("click",(e)=>{ const b=d.querySelector(".body"); b.classList.toggle("collapsed"); e.target.textContent = b.classList.contains("collapsed") ? "Show more ("+it.text.length+" chars)" : "Show less"; }); }
    feed.appendChild(d);
  });
  // apply tail window in full view: keep last N message cards
  if (tailN > 0){
    const cards = Array.from(feed.querySelectorAll(".msg"));
    const drop = Math.max(0, cards.length - tailN);
    for (let i=0;i<drop;i++) cards[i].remove();
    shown = cards.length - drop >= 0 ? feed.querySelectorAll(".msg").length : shown;
  }
  if (newestFirst){ const cards = Array.from(feed.querySelectorAll(".msg")).reverse(); cards.forEach((c)=>feed.appendChild(c)); }
  const fullTok = lastStats ? lastStats.est_tokens : tokens(lastText||"");
  document.getElementById("stats").textContent =
    "Messages: "+feed.querySelectorAll(".msg").length+" shown ("+AGENTS.map((a)=>a+" "+(counts[a]||0)).join(" · ")+") · type="+typeF+" · tail="+(tailN||"all");
  document.getElementById("tokenmeter").innerHTML =
    "View <b>"+tokens(feed.innerText)+"</b> tokens~ vs full <b>"+fullTok+"</b> tokens~";
  if (stick) window.scrollTo(0, document.body.scrollHeight);
}
let lastSeenLine = -1, lastFetchGame = null, lastFetchMode = "", polling = false;
async function poll(){
  if (paused || polling) return;
  polling = true;
  try {
    await refreshGames();
    if (!currentGame) return;
    if (currentGame !== lastTreeGame){ lastTreeGame = currentGame; loadTree(); }
    const s = await (await fetch("/stats?game="+encodeURIComponent(currentGame)+"&t="+Date.now(),{cache:"no-store"})).json();
    lastStats = s;
    const freshGame = currentGame !== lastFetchGame || viewMode !== lastFetchMode;
    if (!freshGame && lastText !== null && s.last_line === lastSeenLine){
      document.getElementById("status").innerHTML = '<span class="live">● live</span>';
      return; // nothing new: skip the heavy fetch + full re-render
    }
    if (viewMode === "brief"){
      const b = await (await fetch("/brief?game="+encodeURIComponent(currentGame)+"&t="+Date.now(),{cache:"no-store"})).json();
      lastBrief = b; lastText = b.text;
      document.getElementById("status").innerHTML = '<span class="live">● live</span>';
      render(parse("Human: brief placeholder\\n"));
    } else {
      const r = await fetch("/messages?game="+encodeURIComponent(currentGame)+"&t="+Date.now(),{cache:"no-store"});
      const t = await r.text();
      document.getElementById("status").innerHTML = '<span class="live">● live</span>';
      lastText = t; lastItems = parse(t); render(lastItems);
    }
    lastSeenLine = s.last_line; lastFetchGame = currentGame; lastFetchMode = viewMode;
  } catch(e){ document.getElementById("status").textContent = "disconnected - retrying"; }
  finally { polling = false; }
}
function restartPoll(){ if (pollTimer) clearInterval(pollTimer); if (refreshMs>0) pollTimer = setInterval(poll, refreshMs); }
document.querySelectorAll(".chip[data-agent]").forEach((b)=>{
  b.addEventListener("click", ()=>{
    const a=b.getAttribute("data-agent");
    if (hidden.has(a)){ hidden.delete(a); b.classList.remove("off"); }
    else { hidden.add(a); b.classList.add("off"); }
    if (lastItems.length) render(lastItems);
  });
});
document.querySelectorAll(".chip[data-view]").forEach((b)=>{
  b.addEventListener("click", ()=>{
    document.querySelectorAll(".chip[data-view]").forEach((x)=>x.classList.remove("on"));
    b.classList.add("on"); viewMode=b.getAttribute("data-view"); poll();
  });
});
document.getElementById("pause").addEventListener("click",(e)=>{ paused=!paused; e.target.textContent=paused?"Resume live":"Pause live"; e.target.classList.toggle("off",paused); });
document.getElementById("search").addEventListener("input",(e)=>{ query=e.target.value.toLowerCase(); if (lastItems.length) render(lastItems); });
document.getElementById("tailsel").addEventListener("change",(e)=>{ tailN=parseInt(e.target.value,10)||0; if (lastItems.length) render(lastItems); });
document.getElementById("typesel").addEventListener("change",(e)=>{ typeF=e.target.value; if (lastItems.length) render(lastItems); });
document.getElementById("collapsebtn").addEventListener("click",(e)=>{ collapseLong=!collapseLong; e.target.classList.toggle("on",collapseLong); if (lastItems.length) render(lastItems); });
document.getElementById("newestbtn").addEventListener("click",(e)=>{ newestFirst=!newestFirst; e.target.textContent="Newest first: "+(newestFirst?"on":"off"); e.target.classList.toggle("on",newestFirst); if (lastItems.length) render(lastItems); });
document.getElementById("refsel").addEventListener("change",(e)=>{ refreshMs=parseInt(e.target.value,10)||0; restartPoll(); });
document.getElementById("cancelgame").addEventListener("click", closeNewGame);
document.getElementById("creategame").addEventListener("click", createGame);
document.getElementById("newgametitle").addEventListener("keydown",(e)=>{ if(e.key==="Enter") createGame(); if(e.key==="Escape") closeNewGame(); });
async function copyText(s, okMsg){
  try { await navigator.clipboard.writeText(s); } catch(e){
    const ta=document.createElement("textarea"); ta.value=s; document.body.appendChild(ta); ta.select(); try{document.execCommand("copy");}catch(_){} ta.remove();
  }
}
document.getElementById("copyview").addEventListener("click", async ()=>{
  await copyText(document.getElementById("feed").innerText.slice(0,60000));
});
document.getElementById("copydigest").addEventListener("click", async ()=>{
  try {
    const r = await fetch("/digest?game="+encodeURIComponent(currentGame)+"&format=text&per_msg=180&t="+Date.now());
    await copyText(await r.text());
  } catch(e){}
});
document.getElementById("dljson").addEventListener("click", async ()=>{
  const r = await fetch("/messages?game="+encodeURIComponent(currentGame)+"&format=json&t="+Date.now());
  const blob = new Blob([await r.text()],{type:"application/json"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = currentGame.replace(/\\.txt$/,"")+".json"; a.click();
});
document.getElementById("dldigest").addEventListener("click", async ()=>{
  const r = await fetch("/digest?game="+encodeURIComponent(currentGame)+"&format=text&per_msg=180&t="+Date.now());
  const blob = new Blob([await r.text()],{type:"text/plain"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = currentGame.replace(/\\.txt$/,"")+"-digest.txt"; a.click();
});
document.getElementById("jumptop").addEventListener("click", ()=>window.scrollTo(0,0));
document.getElementById("jumplatest").addEventListener("click", ()=>window.scrollTo(0,document.body.scrollHeight));
document.getElementById("protocolbtn").addEventListener("click", async ()=>{
  const p = document.getElementById("protocolpanel");
  const showing = p.style.display !== "none";
  if (showing){ p.style.display = "none"; return; }
  p.style.display = "block";
  try {
    const r = await fetch("/guide?t="+Date.now(), {cache:"no-store"});
    document.getElementById("protocoltext").textContent = await r.text();
  } catch(e){ document.getElementById("protocoltext").textContent = "Could not load /guide."; }
});
document.getElementById("closeprotocol").addEventListener("click", ()=>{ document.getElementById("protocolpanel").style.display = "none"; });
document.getElementById("dlappbtn").addEventListener("click", ()=>{ window.location.href = "/download"; });
let delStep = 1;
function showDelStep(){ [1,2,3].forEach((i)=>{ document.getElementById("delstep"+i).style.display = i===delStep ? "block" : "none"; }); }
document.getElementById("delbtn").addEventListener("click", async ()=>{
  await refreshGames();
  const sel = document.getElementById("delsel");
  sel.innerHTML = "";
  games.forEach((g)=>{
    const o = document.createElement("option");
    o.value = g.id;
    o.textContent = g.label + " (" + g.messages + " msgs)";
    if (g.id === "AI INTERCONNECTION CENTER.txt"){ o.disabled = true; o.textContent += " — protected, cannot delete"; }
    sel.appendChild(o);
  });
  document.getElementById("delack").checked = false;
  document.getElementById("delname").value = "";
  document.getElementById("delword").value = "";
  document.getElementById("delerr").textContent = "";
  delStep = 1; showDelStep();
  document.getElementById("delmodal").style.display = "flex";
});
document.getElementById("delclose").addEventListener("click", ()=>{ document.getElementById("delmodal").style.display = "none"; });
document.getElementById("delnext1").addEventListener("click", ()=>{
  if (!document.getElementById("delack").checked){ document.getElementById("delerr").textContent = "Step 1/3: tick the box first."; return; }
  document.getElementById("delerr").textContent = "";
  document.getElementById("delwant").textContent = "Expected: " + document.getElementById("delsel").value;
  delStep = 2; showDelStep();
});
document.getElementById("delback2").addEventListener("click", ()=>{ delStep = 1; showDelStep(); });
document.getElementById("delnext2").addEventListener("click", ()=>{
  if (!document.getElementById("delname").value){ document.getElementById("delerr").textContent = "Step 2/3: retype the exact file name."; return; }
  document.getElementById("delerr").textContent = "";
  delStep = 3; showDelStep();
});
document.getElementById("delback3").addEventListener("click", ()=>{ delStep = 2; showDelStep(); });
document.getElementById("delgo").addEventListener("click", async ()=>{
  const err = document.getElementById("delerr");
  try {
    const r = await fetch("/delete_game", {
      method: "POST", headers: {"Content-Type":"application/json"},
      body: JSON.stringify({
        game: document.getElementById("delsel").value,
        confirm_understand: document.getElementById("delack").checked,
        confirm_name: document.getElementById("delname").value,
        confirm_irreversible: document.getElementById("delword").value,
      }),
    });
    const res = await r.json();
    if (!res.ok){ err.textContent = res.error || "Delete failed."; return; }
    document.getElementById("delmodal").style.display = "none";
    lastText = null; lastFetchGame = null;
    await refreshGames(); poll();
  } catch(e){ err.textContent = "Server unreachable."; }
});
document.getElementById("copyprotocol").addEventListener("click", async ()=>{
  await copyText(document.getElementById("protocoltext").textContent.slice(0,60000));
});
async function sendMessage(){
  const box=document.getElementById("composer"), err=document.getElementById("composererr");
  const text=box.value.trim();
  if(!text) return;
  if(text.length>2000){ err.textContent="Message too long (max 2000 chars)."; return; }
  if(!currentGame){ err.textContent="No channel selected."; return; }
  try {
    const r=await fetch("/messages?game="+encodeURIComponent(currentGame),{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({text})});
    const res=await r.json();
    if(!res.ok){ err.textContent=res.error||"Send failed."; return; }
    box.value=""; err.textContent=""; poll();
  } catch(e){ err.textContent="Server unreachable."; }
}
document.getElementById("sendbtn").addEventListener("click", sendMessage);
document.getElementById("composer").addEventListener("keydown",(e)=>{ if((e.ctrlKey||e.metaKey)&&e.key==="Enter") sendMessage(); });
document.addEventListener("keydown",(e)=>{
  if (e.target.tagName==="INPUT"||e.target.tagName==="TEXTAREA") return;
  if (e.key==="/"){ e.preventDefault(); document.getElementById("search").focus(); }
  else if (e.key==="c"){ document.getElementById("copydigest").click(); }
  else if (e.key==="1"){ document.querySelector('.chip[data-view="full"]').click(); }
  else if (e.key==="2"){ document.querySelector('.chip[data-view="compact"]').click(); }
  else if (e.key==="3"){ document.querySelector('.chip[data-view="brief"]').click(); }
});
async function loadTree(){
  if (!currentGame) return;
  try {
    const r = await fetch("/tree?game="+encodeURIComponent(currentGame)+"&t="+Date.now(), {cache:"no-store"});
    if (!r.ok){ treeData = null; renderTree(); return; }
    treeData = await r.json();
    treeCollapsed = new Set();
    autoCollapse(treeData.tree, "", 0);
    renderTree();
  } catch(e){ treeData = null; renderTree(); }
}
function autoCollapse(nodes, prefix, depth){
  (nodes||[]).forEach((n,i)=>{
    const id = prefix ? prefix+"."+i : ""+i;
    if (depth >= 1 && n.children && n.children.length) treeCollapsed.add(id);
    autoCollapse(n.children||[], id, depth+1);
  });
}
function treeMatches(n, q){
  const s = ((n.title||"")+" "+(n.detail||"")+" "+((n.refs||[]).join(" "))).toLowerCase();
  if (s.indexOf(q) >= 0) return true;
  return (n.children||[]).some((k)=>treeMatches(k, q));
}
function renderTreeNode(n, id, depth){
  const hasKids = n.children && n.children.length;
  const shut = treeCollapsed.has(id);
  let h = '<div class="tnode"><div class="trow" data-id="'+id+'"><span style="color:var(--dim)">'+(hasKids ? (shut ? "&#9656;" : "&#9662;") : "&middot;")+'</span> ';
  h += '<span class="ttitle">'+esc(n.title||"")+'</span>';
  if (n.detail) h += ' <span class="tdetail">&mdash; '+esc(n.detail)+'</span>';
  if (n.refs && n.refs.length) h += ' <span class="trefs">['+esc(n.refs.slice(0,6).join(", ")) +']</span>';
  h += '</div>';
  if (hasKids && !shut){
    h += '<div class="tkids">';
    n.children.forEach((k,i)=>{ h += renderTreeNode(k, id+"."+i, depth+1); });
    h += '</div>';
  }
  return h + '</div>';
}
function renderTree(){
  const box = document.getElementById("tree"), meta = document.getElementById("treemeta");
  if (!treeData || !treeData.tree || !treeData.tree.length){
    meta.textContent = "No tree yet for this channel — agents: seed it via POST /tree.";
    box.innerHTML = ""; return;
  }
  meta.textContent = "v"+treeData.version+" · "+treeData.nodes+" nodes · ~"+treeData.est_tokens+" tok · by "+(treeData.updated_by||"?")+" "+(treeData.updated_at||"");
  const q = treeFilterQ.trim().toLowerCase();
  let nodes = treeData.tree;
  if (q) nodes = nodes.filter((n)=>treeMatches(n, q));
  if (!nodes.length){ box.innerHTML = '<div style="color:var(--dim);font-size:12px;">No matches.</div>'; return; }
  let h = "";
  nodes.forEach((n,i)=>{ h += renderTreeNode(n, ""+i, 0); });
  box.innerHTML = h;
  box.querySelectorAll(".trow").forEach((row)=>{
    row.addEventListener("click", ()=>{
      const id = row.getAttribute("data-id");
      if (treeCollapsed.has(id)) treeCollapsed.delete(id); else treeCollapsed.add(id);
      renderTree();
    });
  });
}
document.getElementById("treefilter").addEventListener("input", (e)=>{ treeFilterQ = e.target.value; renderTree(); });
document.getElementById("treeexpand").addEventListener("click", ()=>{ treeCollapsed = new Set(); renderTree(); });
document.getElementById("treecollapse").addEventListener("click", ()=>{
  treeCollapsed = new Set();
  const walk = (nodes, prefix)=>{ (nodes||[]).forEach((n,i)=>{ const id = prefix ? prefix+"."+i : ""+i; if (n.children && n.children.length){ treeCollapsed.add(id); walk(n.children, id); } }); };
  if (treeData) walk(treeData.tree, "");
  renderTree();
});
document.getElementById("treecopy").addEventListener("click", async ()=>{
  try {
    const r = await fetch("/tree?game="+encodeURIComponent(currentGame)+"&format=text&t="+Date.now());
    await copyText(await r.text());
  } catch(e){}
});
document.getElementById("banner-tree").addEventListener("click", (e)=>{
  e.preventDefault();
  window.open("/tree?game="+encodeURIComponent(currentGame||"")+"&format=text", "_blank");
});
setInterval(()=>{ if (!paused && currentGame) loadTree(); }, 30000);
document.getElementById("deepsearchbtn").addEventListener("click", async ()=>{
  const kw = document.getElementById("deepq").value.trim();
  const box = document.getElementById("deepresults");
  if (!kw){ box.innerHTML = '<div style="color:var(--dim);font-size:12px;">Type a keyword first.</div>'; return; }
  const ag = document.getElementById("deepagent").value, ty = document.getElementById("deeptype").value;
  const mx = parseInt(document.getElementById("deepmax").value, 10) || 10;
  box.innerHTML = '<div style="color:var(--dim);font-size:12px;">searching...</div>';
  try {
    let url = "/messages?game="+encodeURIComponent(currentGame)+"&format=json&q="+encodeURIComponent(kw);
    if (ag) url += "&agent="+encodeURIComponent(ag);
    if (ty && ty !== "all") url += "&type="+encodeURIComponent(ty);
    const j = await (await fetch(url, {cache:"no-store"})).json();
    const hits = (j.messages||[]).slice(-mx);
    if (!hits.length){ box.innerHTML = '<div style="color:var(--dim);font-size:12px;">No hits in full history.</div>'; return; }
    box.innerHTML = '<div style="color:var(--dim);font-size:12px;">'+j.count+' hit(s) in full history (~'+j.est_tokens+' tok) — showing '+hits.length+':</div>' +
      hits.map((m, i)=>{
        const one = m.text.replace(/\\s+/g, " ").trim();
        const snip = one.length > 220 ? one.slice(0, 220)+"..." : one;
        return '<div class="deephit" data-i="'+i+'"><b>'+m.agent+'</b> <span class="typepill">'+m.type+'</span> '
          + '<span class="line">L'+m.line+'</span><div>'+esc(snip)+'</div>'
          + '<span class="more">Show full ('+m.text.length+' chars)</span>'
          + '<div class="body" style="display:none; white-space:pre-wrap; margin-top:4px;">'+fmtBody(m.text.trim())+'</div></div>';
      }).join("");
    box.querySelectorAll(".deephit").forEach((d)=>{
      d.querySelector(".more").addEventListener("click", ()=>{
        const b = d.querySelector(".body"), open = b.style.display !== "none";
        b.style.display = open ? "none" : "block";
        d.querySelector(".more").textContent = open ? "Show full" : "Hide full";
      });
    });
    box.dataset.full = JSON.stringify(hits.map((m)=>({line:m.line, agent:m.agent, type:m.type, text:m.text})));
  } catch(e){ box.innerHTML = '<div style="color:var(--dim);font-size:12px;">Search failed.</div>'; }
});
document.getElementById("deepcopybtn").addEventListener("click", async ()=>{
  const raw = document.getElementById("deepresults").dataset.full;
  if (!raw) return;
  const hits = JSON.parse(raw);
  await copyText(hits.map((m)=>"L"+m.line+" ["+m.agent+"]["+m.type+"] "+m.text.replace(/\\s+/g," ").trim().slice(0,400)).join("\\n"));
});
document.getElementById("deepq").addEventListener("keydown", (e)=>{ if (e.key === "Enter"){ e.preventDefault(); document.getElementById("deepsearchbtn").click(); } });
document.getElementById("briefbtn").addEventListener("click", async ()=>{
  const box = document.getElementById("composer"), err = document.getElementById("composererr");
  if (!box.value.trim()){ err.textContent = "Nothing to condense."; return; }
  try {
    const r = await fetch("/brief-it", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({text: box.value})});
    const j = await r.json();
    box.value = j.brief;
    err.textContent = "Briefed: " + j.orig_tokens + " -> " + j.brief_tokens + " tok (" + j.saved_pct + "% saved). Code/numbers/negations untouched.";
  } catch(e){ err.textContent = "Brief-it failed."; }
});
document.getElementById("kmapbtn").addEventListener("click", async ()=>{
  const p = document.getElementById("kmappanel");
  if (p.style.display !== "none"){ p.style.display = "none"; return; }
  p.style.display = "block";
  const meta = document.getElementById("kmapmeta"), body = document.getElementById("kmapbody");
  meta.textContent = "loading..."; body.innerHTML = "";
  try {
    const j = await (await fetch("/kmap?game="+encodeURIComponent(currentGame)+"&t="+Date.now(), {cache:"no-store"})).json();
    meta.textContent = j.nodes + " nodes · " + j.edges + " edges · " + j.messages + " msgs";
    body.innerHTML = "<div><b>Gods</b> (most-connected): " + j.gods.map((g)=>esc(g.label)+" <span style='color:var(--dim)'>"+g.degree+"</span>").join(" · ") + "</div>"
      + "<div style='margin-top:6px;'><b>Communities</b>:</div>"
      + j.communities.slice(0, 8).map((c)=>"<div style='margin-left:10px;'>· " + c.members.map(esc).join(", ") + "</div>").join("");
  } catch(e){ meta.textContent = "kmap failed."; }
});
document.getElementById("kmapclose").addEventListener("click", ()=>{ document.getElementById("kmappanel").style.display = "none"; });
document.getElementById("kmapgobtn").addEventListener("click", async ()=>{
  const q = document.getElementById("kmapq").value.trim();
  const body = document.getElementById("kmapbody");
  if (!q) return;
  body.innerHTML = "<div style='color:var(--dim)'>asking...</div>";
  try {
    const j = await (await fetch("/kmap/query?game="+encodeURIComponent(currentGame)+"&q="+encodeURIComponent(q), {cache:"no-store"})).json();
    let h = "";
    (j.hits||[]).forEach((x)=>{
      h += "<div class='deephit'><b>"+esc(x.label)+"</b> <span class='typepill'>"+x.kind+" · deg "+x.degree+"</span>"
        + (x.refs||[]).map((r)=>"<div style='margin-left:10px;'><span class='line'>L"+r.line+" "+esc(r.agent)+"</span> "+esc(r.snippet)+"</div>").join("") + "</div>";
    });
    (j.messages||[]).forEach((m)=>{
      h += "<div class='deephit'><span class='line'>L"+m.line+" "+esc(m.agent)+"</span> "+esc(m.snippet)+"</div>";
    });
    body.innerHTML = h || "<div style='color:var(--dim)'>No hits.</div>";
  } catch(e){ body.innerHTML = "<div style='color:var(--dim)'>Query failed.</div>"; }
});
document.getElementById("kmapq").addEventListener("keydown", (e)=>{ if (e.key === "Enter"){ e.preventDefault(); document.getElementById("kmapgobtn").click(); } });
restartPoll(); poll();
</script>
</body>
</html>
"""


def append_human_message(game, text):
    """Append a Human message to a channel. Returns (ok, error)."""
    agents = AGENTS
    if not isinstance(text, str):
        return None, "Empty message."
    clean = text.replace("\r", "").strip()
    if not clean:
        return None, "Empty message."
    if len(clean) > 2000:
        return None, "Message too long (max 2000 chars)."
    guarded = []
    for line in clean.split("\n"):
        if AGENT_RE.match(line):
            guarded.append("> " + line)
        else:
            guarded.append(line)
    path = None
    for g in list_games():
        if g["id"] == game:
            path = os.path.join(ROOT, g["id"])
            break
    if path is None:
        return None, "Unknown channel."
    with open(path, "a", encoding="utf-8") as f:
        f.write("\nHuman: " + "\n".join(guarded) + "\n")
    return True, ""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_bytes(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8")

    def do_GET(self):
        parts = urlparse(self.path)
        query = parse_qs(parts.query)
        game = (query.get("game") or [None])[0]

        if parts.path == "/health":
            self.send_json({"ok": True, "port": PORT, "version": APP_VERSION, "games": len(list_games())})
            return

        if parts.path == "/download":
            zp = os.path.join(ROOT, "dist", "AI-Interconnection-Center-%s.zip" % APP_VERSION)
            try:
                with open(zp, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", 'attachment; filename="AI-Interconnection-Center.zip"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parts.path == "/tree":
            t, _path = load_tree(game)
            if t is None:
                self.send_response(404)
                self.end_headers()
                return
            nodes = sum(1 for _ in _walk_tree(t.get("roots", [])))
            fmt = (query.get("format") or ["json"])[0]
            if fmt == "text":
                try:
                    mc = max(1000, min(int((query.get("max_chars") or ["12000"])[0]), 60000))
                except ValueError:
                    mc = 12000
                body = ("# TREE %s (v%d, %d nodes) - project context, read before chat history\n# full protocol: GET /guide\n\n"
                        % (game or "", t.get("version", 0), nodes)) + render_tree_text(t.get("roots", []), max_chars=mc)
                self.send_bytes(body.encode("utf-8"), "text/plain; charset=utf-8")
            else:
                raw = json.dumps(t.get("roots", []), ensure_ascii=False)
                self.send_json({"game": game, "version": t.get("version", 0),
                                "updated_at": t.get("updated_at", ""),
                                "updated_by": t.get("updated_by", ""),
                                "nodes": nodes, "est_tokens": est_tokens(len(raw)),
                                "tree": t.get("roots", [])})
            return

        if parts.path == "/kmap" or parts.path.startswith("/kmap/"):
            sub = parts.path[len("/kmap"):].strip("/")
            km = build_kmap(game)
            if sub == "":
                self.send_json({"game": game, "messages": km["messages"],
                                "nodes": km["node_count"], "edges": km["edge_count"],
                                "est_tokens": km["est_tokens"],
                                "gods": km["gods"], "communities": km["communities"]})
            elif sub == "query":
                q = ((query.get("q") or [""])[0])
                self.send_json({"game": game, "q": q,
                                **kmap_query(km, q)})
            elif sub == "path":
                a = ((query.get("a") or [""])[0])
                b = ((query.get("b") or [""])[0])
                res = kmap_path(km, a, b)
                self.send_response(200 if "error" not in res else 400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps(res).encode("utf-8"))
            elif sub == "explain":
                res = kmap_explain(km, (query.get("node") or [""])[0])
                self.send_response(200 if "error" not in res else 400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(json.dumps(res).encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()
            return

        if parts.path == "/guide":
            fmt = (query.get("format") or ["text"])[0]
            if fmt == "json":
                self.send_json({"est_tokens": est_tokens(len(GUIDE_TEXT)),
                                "chars": len(GUIDE_TEXT), "text": GUIDE_TEXT})
            else:
                self.send_bytes(GUIDE_TEXT.encode("utf-8"), "text/plain; charset=utf-8")
            return

        if parts.path == "/games":
            self.send_json(list_games())
            return

        if parts.path == "/stats":
            text, _path = read_channel_text(game)
            items = parse_channel(text)
            self.send_json(build_stats(items, len(text)))
            return

        if parts.path == "/digest":
            text, _path = read_channel_text(game)
            items = parse_channel(text)
            try:
                per_msg = max(40, min(int((query.get("per_msg") or ["180"])[0]), 500))
            except ValueError:
                per_msg = 180
            try:
                max_msgs = int((query.get("max_msgs") or ["0"])[0]) or 0
            except ValueError:
                max_msgs = 0
            agent = (query.get("agent") or [None])[0]
            q = (query.get("q") or [None])[0]
            msg_type = (query.get("type") or [None])[0]
            items = filter_items(items, agent=agent, q=q, msg_type=msg_type)
            if max_msgs > 0:
                items = items[-max_msgs:]
            digest = build_digest(items, per_msg=per_msg)
            fmt = (query.get("format") or ["json"])[0]
            if fmt == "text":
                lines = ["# DIGEST %s (%d messages, one line each)" % (game or "", len(digest)),
                         "# protocol: GET /guide once, then /stats -> /digest -> /brief -> /messages?tail=N",
                         ""]
                for d in digest:
                    tags = (" [" + "|".join(d["tags"][:3]) + "]") if d["tags"] else ""
                    lines.append("L%(line)d [%(agent)s][%(type)s]%(tags)s %(snippet)s" % {
                        "line": d["line"], "agent": d["agent"],
                        "type": d["type"], "tags": tags, "snippet": d["snippet"]})
                self.send_bytes(("\n".join(lines)).encode("utf-8"), "text/plain; charset=utf-8")
            else:
                self.send_json({"game": game, "count": len(digest), "per_msg": per_msg,
                                "est_tokens": est_tokens(sum(len(d["snippet"]) for d in digest)),
                                "digest": digest})
            return

        if parts.path == "/brief":
            text, _path = read_channel_text(game)
            items = parse_channel(text)
            try:
                recent = max(1, min(int((query.get("recent") or ["3"])[0]), 10))
            except ValueError:
                recent = 3
            try:
                budget = max(2000, min(int((query.get("budget_chars") or ["16000"])[0]), 60000))
            except ValueError:
                budget = 16000
            brief = build_brief(items, len(text), recent=recent, budget_chars=budget)
            fmt = (query.get("format") or ["json"])[0]
            if fmt == "text":
                self.send_bytes(brief["text"].encode("utf-8"), "text/plain; charset=utf-8")
            else:
                self.send_json({"game": game, **brief})
            return

        if parts.path == "/messages":
            text, _path = read_channel_text(game)
            fmt = (query.get("format") or ["text"])[0]
            tail = (query.get("tail") or [None])[0]
            from_line = (query.get("from_line") or [None])[0]
            max_chars = (query.get("max_chars") or [None])[0]
            q = (query.get("q") or [None])[0]
            agent = (query.get("agent") or [None])[0]
            msg_type = (query.get("type") or [None])[0]
            # No filters at all -> exact legacy behavior (full raw file).
            if tail is None and from_line is None and max_chars is None and q is None \
                    and agent is None and msg_type is None and fmt == "text" \
                    and "t" in query and len(query) == 2:
                # poll() appends &t=timestamp; strip nothing, return raw file
                self.send_bytes(text.encode("utf-8"), "text/plain; charset=utf-8")
                return
            if tail is None and from_line is None and max_chars is None and q is None \
                    and agent is None and msg_type is None:
                self.send_bytes(text.encode("utf-8"), "text/plain; charset=utf-8")
                return
            items = parse_channel(text)
            items = filter_items(items, agent=agent, q=q, msg_type=msg_type)
            if from_line is not None:
                try:
                    fl = int(from_line)
                    items = [it for it in items if it["line"] >= fl]
                except ValueError:
                    pass
            if tail is not None:
                try:
                    n = int(tail)
                    if n > 0:
                        items = items[-n:]
                except ValueError:
                    pass
            if fmt == "json":
                payload = {"game": game, "count": len(items),
                           "est_tokens": est_tokens(sum(it["chars"] for it in items)),
                           "messages": [{"line": it["line"], "agent": it["agent"],
                                         "type": it["type"], "tags": it["tags"],
                                         "tokens": it["est_tokens"], "text": it["text"]} for it in items]}
                self.send_json(payload)
                return
            # text rendering of the filtered window
            parts_out = []
            for it in items:
                parts_out.append("%s: %s" % (it["agent"], it["text"]))
            body = "\n\n".join(parts_out)
            if max_chars is not None:
                try:
                    mc = max(500, min(int(max_chars), 200000))
                    if len(body) > mc:
                        # keep the tail (most recent) when truncating
                        body = "...[truncated oldest-first, %d chars shown]...\n\n" % mc + body[-mc:]
                except ValueError:
                    pass
            self.send_bytes(body.encode("utf-8"), "text/plain; charset=utf-8")
            return

        self.send_bytes(PAGE.encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        parts = urlparse(self.path)
        if parts.path == "/delete_game":
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            try:
                payload = json.loads(self.rfile.read(max(0, length)).decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            data, error = delete_game(payload.get("game"),
                                      payload.get("confirm_understand"),
                                      payload.get("confirm_name"),
                                      payload.get("confirm_irreversible"))
            if data:
                status = 200
            elif error == "Unknown channel.":
                status = 404
            elif error.startswith("The default channel"):
                status = 403
            else:
                status = 400
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            out = {"ok": bool(data), "error": error or ""}
            if data:
                out.update(data)
            self.wfile.write(json.dumps(out).encode("utf-8"))
            return
        if parts.path == "/tree":
            query = parse_qs(parts.query)
            game = (query.get("game") or [None])[0]
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            try:
                payload = json.loads(self.rfile.read(max(0, length)).decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            roots = payload.get("tree", payload.get("roots"))
            t, error = save_tree(game, payload.get("agent", ""), roots, payload.get("expected_version"))
            is_conflict = bool(error) and error.startswith("Version conflict")
            self.send_response(200 if t else (409 if is_conflict else 400))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            out = {"ok": bool(t), "error": error or ""}
            if t:
                out["version"] = t["version"]
            self.wfile.write(json.dumps(out).encode("utf-8"))
            return
        if parts.path == "/brief-it":
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            try:
                payload = json.loads(self.rfile.read(max(0, length)).decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            try:
                mc = max(200, min(int(payload.get("max_chars", 2000)), 8000))
            except (TypeError, ValueError):
                mc = 2000
            self.send_json({"ok": True,
                            **brief_text(payload.get("text", ""), max_chars=mc)})
            return
        if parts.path == "/simple":
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            try:
                payload = json.loads(self.rfile.read(max(0, length)).decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            code = payload.get("code", "")
            if not isinstance(code, str) or len(code) > 60000:
                self.send_response(400)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(json.dumps({"ok": False,
                                             "error": "Send code as a string (max 60000 chars)."}).encode("utf-8"))
                return
            self.send_json({"ok": True, **simplicity_check(code)})
            return
        if parts.path == "/agent_append":
            query = parse_qs(parts.query)
            game = (query.get("game") or [None])[0]
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            try:
                payload = json.loads(self.rfile.read(max(0, length)).decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            ok, error = append_agent_message(game, payload.get("agent", ""), payload.get("text", ""))
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": bool(ok), "error": error or ""}).encode("utf-8"))
            return
        if parts.path == "/messages":
            query = parse_qs(parts.query)
            game = (query.get("game") or [None])[0]
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = 0
            try:
                payload = json.loads(self.rfile.read(max(0, length)).decode("utf-8") or "{}")
            except ValueError:
                payload = {}
            ok, error = append_human_message(game, payload.get("text", ""))
            self.send_response(200 if ok else 400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": bool(ok), "error": error or ""}).encode("utf-8"))
            return
        if parts.path != "/new_game":
            self.send_response(404)
            self.end_headers()
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        try:
            payload = json.loads(self.rfile.read(max(0, length)).decode("utf-8") or "{}")
        except ValueError:
            payload = {}
        game_id, created = create_game(payload.get("title", ""))
        if game_id is None:
            self.send_response(400)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps({
                "ok": False,
                "error": "Title must start with a letter or digit, up to 61 chars of letters/digits/spaces/_/-/'.",
            }).encode("utf-8"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "id": game_id, "created": created}).encode("utf-8"))


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    with ThreadedTCPServer(("", PORT), Handler) as httpd:
        print(f"Channel viewer running at http://127.0.0.1:{PORT}")
        print("AI protocol: GET /guide once, then poll /stats -> /digest -> /brief -> /messages?tail=N")
        httpd.serve_forever()
