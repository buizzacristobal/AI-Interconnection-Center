"""AI Interconnection Center - coordination hub for several AI coding agents + one human.

Run:  python channel_viewer.py            then open http://127.0.0.1:19843
Env:  AIC_PORT (19843)  AIC_HOST (127.0.0.1; 0.0.0.0 = LAN, turn on access keys)
      AIC_HOME (folder holding the channel files; default: this folder)

Agents: one call per turn  ->  GET /sync?game=<id>&agent=<Name>&since=<cursor>
        full protocol      ->  GET /guide
The channel .txt files stay the source of truth: plain "Agent: text" blocks
that any tool can append to or grep. Everything else (tasks, claims,
timestamps, config) lives in .aic/ next to them. Standard library only.
"""

import http.server
import json
import os
import re
import socketserver
import sys
import threading
import time
import traceback
from urllib.parse import urlparse, parse_qs, quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import aic_store as store  # noqa: E402
import aic_llm as llm  # noqa: E402

APP_VERSION = "3.0.0"
PORT = int(os.environ.get("AIC_PORT", "19843"))
HOST = os.environ.get("AIC_HOST", "127.0.0.1")
ROOT = store.HOME
APP_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(APP_DIR, "web")
CHANNEL_PREFIX = "AI INTERCONNECTION CENTER"
DEFAULT_CHANNEL_ID = CHANNEL_PREFIX + ".txt"
CHANNEL_FILE = os.path.join(ROOT, DEFAULT_CHANNEL_ID)
HUMAN = store.HUMAN

LINE_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_\-]{1,23}):\s?(.*)$")
TAG_RE = re.compile(r"\[([^\]\n]{2,60})\]")
MENTION_RE = re.compile(r"(?<![\w@/.])@([A-Za-z][A-Za-z0-9_\-]{1,23})")
LEGACY_TO_RE = re.compile(r"\[TO:\s*([^\]\n]{1,200})\]", re.I)
TYPES = ["General", "Change Notice", "Decision", "Question", "Blocker", "Handoff", "Review",
         "Fix", "Verify", "Status", "Advisory", "Discussion", "Protocol", "VFX", "Tool"]
POST_TYPES = ["General", "Change Notice", "Decision", "Question", "Blocker", "Handoff", "Review", "Status"]


def base_url():
    host = "127.0.0.1" if HOST in ("", "0.0.0.0", "::") else HOST
    return "http://%s:%d" % (host, PORT)


def est_tokens(n_chars):
    """~4 chars/token heuristic for English. Documented, not exact."""
    return max(1, int(n_chars / 4))


def match_agent_line(line):
    """(canonical agent, rest) when a line opens a message, else None.

    Any casing or configured alias counts ('DeepSeek:' -> Deepseek), but only
    for names on the roster: 'FILES: ...' inside a message stays a body line.
    """
    m = LINE_RE.match(line)
    if not m:
        return None
    canon = store.resolve_agent(m.group(1))
    return (canon, m.group(2)) if canon else None


# ---------------------------------------------------------------- channels

def channel_ids():
    try:
        files = sorted(f for f in os.listdir(ROOT)
                       if f.startswith(CHANNEL_PREFIX) and f.lower().endswith(".txt")
                       and os.path.isfile(os.path.join(ROOT, f)))
    except OSError:
        files = []
    if DEFAULT_CHANNEL_ID in files:
        files.remove(DEFAULT_CHANNEL_ID)
        files.insert(0, DEFAULT_CHANNEL_ID)
    return files


def game_path(game):
    """Real path of a channel, or None. No params -> default channel.

    Only names that exist in the folder resolve, so there is no path
    traversal and no silent fallback to the wrong project.
    """
    if not game:
        game = DEFAULT_CHANNEL_ID
    return os.path.join(ROOT, game) if game in channel_ids() else None


def resolve_game_file(game):
    """Back-compat: path for a channel id (default channel if unknown)."""
    return game_path(game) or CHANNEL_FILE


def read_channel_text(game):
    path = game_path(game)
    if path is None:
        return "", ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(), path
    except FileNotFoundError:
        return "", path


def channel_label(filename):
    """Human label: project.json label, else derived from the file name."""
    label = store.load_project(filename).get("label")
    if label:
        return label
    base = filename[:-4] if filename.lower().endswith(".txt") else filename
    rest = base[len(CHANNEL_PREFIX):].strip()
    if rest.startswith("-"):
        rest = rest[1:].strip()
    return rest or "Main"


def classify_type(text):
    t = text.lower()
    if "change notice" in t or "change_notice" in t:
        return "Change Notice"
    if "blocker" in t or "blocked" in t:
        return "Blocker"
    if "handoff" in t or "hand-off" in t:
        return "Handoff"
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


def normalize_type(t):
    if not t:
        return None
    for name in TYPES:
        if name.lower() == str(t).strip().lower():
            return name
    return ""


def extract_mentions(body):
    out = []
    for m in MENTION_RE.findall(body):
        low = m.lower()
        canon = "all" if low in ("all", "everyone", "team") else store.resolve_agent(m)
        if canon and canon not in out:
            out.append(canon)
    for grp in LEGACY_TO_RE.findall(body[:600]):
        for part in re.split(r"[/,&+]| and ", grp):
            word = part.strip().split(" ")[0] if part.strip() else ""
            canon = "all" if word.lower() in ("all", "everyone", "team") else store.resolve_agent(word)
            if canon and canon not in out:
                out.append(canon)
    return out


def parse_channel(text):
    """Parse channel text into [{agent, text, line, chars, est_tokens, tags, type, mentions, preview}].

    '#' lines are comments only in the header (before the first message);
    later they are content - code comments and markdown headings inside
    a message used to cut the message off.
    """
    lines = text.split("\n")
    items = []
    cur = None
    for i, raw in enumerate(lines):
        line = raw[:-1] if raw.endswith("\r") else raw
        m = match_agent_line(line)
        if m:
            if cur is not None:
                items.append(cur)
            cur = {"agent": m[0], "text": m[1], "line": i + 1}
        elif cur is None:
            continue  # header / preamble
        elif line.strip() == "":
            cur["text"] += "\n"
        else:
            cur["text"] += "\n" + line
    if cur is not None:
        items.append(cur)
    for it in items:
        body = it["text"].strip()
        it["text"] = body
        it["chars"] = len(body)
        it["est_tokens"] = est_tokens(len(body))
        it["tags"] = TAG_RE.findall(body[:600])[:6]
        it["type"] = classify_type(body[:800])
        it["mentions"] = extract_mentions(body)
        first = body.replace("\n", " ").strip()
        it["preview"] = first[:220] + ("..." if len(first) > 220 else "")
        it["ts"] = None
        it["to"] = []
    return items


_items_cache = {}
_items_lock = threading.Lock()


def load_items(game):
    """(items with metadata merged, total chars) for a channel, cached by file stat."""
    path = game_path(game)
    if path is None:
        return [], 0
    game = os.path.basename(path)
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size, store.config_mtime())
    except OSError:
        return [], 0
    try:
        mst = os.stat(store.project_file(game, "meta.jsonl"))
        sig = sig + (mst.st_mtime_ns, mst.st_size)
    except OSError:
        pass
    with _items_lock:
        hit = _items_cache.get(path)
        if hit and hit[0] == sig:
            return hit[1], hit[2]
    text, _ = read_channel_text(game)
    items = parse_channel(text)
    meta = store.load_meta(game)
    for it in items:
        m = meta.get(it["line"])
        if m and m.get("agent") in (None, it["agent"]):
            it["ts"] = m.get("ts")
            if m.get("type"):
                it["type"] = m["type"]
            it["to"] = [x for x in (m.get("to") or []) if isinstance(x, str)]
            for x in it["to"]:
                if x not in it["mentions"]:
                    it["mentions"].append(x)
    with _items_lock:
        _items_cache[path] = (sig, items, len(text))
    return items, len(text)


def list_games():
    games = []
    for f in channel_ids():
        path = os.path.join(ROOT, f)
        items, chars = load_items(f)
        tpath = path + ".tree.json"
        tree_tokens, tree_version = 0, 0
        if os.path.exists(tpath):
            try:
                tree_tokens = est_tokens(os.path.getsize(tpath))
                with open(tpath, "r", encoding="utf-8") as tf:
                    tree_version = int(json.loads(tf.read()).get("version", 0))
            except (OSError, ValueError, TypeError):
                pass
        games.append({
            "id": f, "label": channel_label(f), "bytes": os.path.getsize(path),
            "messages": len(items), "est_tokens": est_tokens(chars),
            "last_line": items[-1]["line"] if items else 0,
            "last_agent": items[-1]["agent"] if items else "",
            "last_ts": next((it["ts"] for it in reversed(items) if it["ts"]), None),
            "has_tree": os.path.exists(tpath), "tree_tokens": tree_tokens, "tree_version": tree_version,
            "default": f == DEFAULT_CHANNEL_ID,
        })
    return games


def filter_items(items, agent=None, q=None, msg_type=None):
    out = items
    if agent:
        want = {store.resolve_agent(a) or a for a in agent.split(",") if a.strip()}
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
        snippet = re.sub(r"\s+", " ", it["text"]).strip()
        if len(snippet) > per_msg:
            snippet = snippet[:per_msg] + "..."
        digest.append({"line": it["line"], "agent": it["agent"], "type": it["type"],
                       "tags": it["tags"], "tokens": est_tokens(len(snippet)), "snippet": snippet})
    return digest


def build_stats(items, total_chars):
    per_agent, per_type = {}, {}
    for it in items:
        per_agent[it["agent"]] = per_agent.get(it["agent"], 0) + 1
        per_type[it["type"]] = per_type.get(it["type"], 0) + 1
    last = items[-1] if items else None
    return {
        "messages": len(items), "chars": total_chars, "est_tokens": est_tokens(total_chars),
        "per_agent": per_agent, "per_type": per_type,
        "first_line": items[0]["line"] if items else 0,
        "last_line": last["line"] if last else 0,
        "last_agent": last["agent"] if last else "",
        "last_preview": last["preview"] if last else "",
        "last_ts": last["ts"] if last else None,
    }


def build_brief(items, total_chars, recent=3, per_msg=180, budget_chars=16000):
    """Stats + last `recent` messages in full + everything older as digest.

    Total is capped at budget_chars so an AI can pull it blind safely.
    """
    recent = max(1, min(recent, 10))
    tail = items[-recent:] if items else []
    older = items[:-recent] if len(items) > recent else []
    digest = build_digest(older, per_msg=per_msg)
    TAIL_CAP = 3000
    tail_render = []
    for t in tail:
        body = t["text"]
        if len(body) > TAIL_CAP:
            body = body[:TAIL_CAP] + "\n...[cut at %d/%d chars, fetch full via /messages?from_line=%d&limit=1]..." % (
                TAIL_CAP, len(t["text"]), t["line"])
        tail_render.append("--- L%d %s [%s] ---\n%s" % (t["line"], t["agent"], t["type"], body))
    digest_text = "\n".join("L%(line)d [%(agent)s][%(type)s] %(snippet)s" % d for d in digest)
    head = (
        "BRIEF chars=%d tokens~%d messages=%d "
        "(showing last %d recent capped at %d chars each, %d older as one-line digest). "
        "One message in full: GET /messages?game=<id>&from_line=<line>&limit=1\n"
        "Per-turn updates: GET /sync?game=<id>&agent=<you>&since=<cursor>   Protocol: GET /guide\n"
        % (total_chars, est_tokens(total_chars), len(items), len(tail), TAIL_CAP, len(digest))
    )
    full_tail = "\n\n".join(tail_render)
    out = head + "\n[DIGEST older]\n" + digest_text + "\n\n[RECENT capped]\n" + full_tail
    if len(out) > budget_chars:
        lines = digest_text.split("\n")
        # drop oldest digest lines in chunks (one at a time is O(n^2) on big channels)
        while lines and len(out) > budget_chars:
            over = len(out) - budget_chars
            drop = 1
            acc = 0
            while drop < len(lines) and acc < over:
                acc += len(lines[drop - 1]) + 1
                drop += 1
            del lines[:max(1, drop - 1)]
            digest_text = "\n".join(lines)
            out = head + "\n[DIGEST older, truncated oldest-first]\n" + digest_text + "\n\n[RECENT capped]\n" + full_tail
    if len(out) > budget_chars:
        out = "...[brief hard-capped at %d chars, newest kept]...\n" % budget_chars + out[-budget_chars:]
    return {
        "stats": build_stats(items, total_chars), "recent_count": len(tail),
        "digest_count": len(digest), "digest_shown": digest_text.count("\n") + (1 if digest_text else 0),
        "text": out, "chars": len(out), "est_tokens": est_tokens(len(out)),
    }


def message_json(it, full=True):
    out = {"line": it["line"], "agent": it["agent"], "type": it["type"], "tags": it["tags"],
           "tokens": it["est_tokens"], "ts": it["ts"], "mentions": it["mentions"]}
    out["text"] = it["text"] if full else it["preview"]
    return out


# ---------------------------------------------------------------- writing

def append_message(game, agent, text, msg_type=None, to=None):
    """Append one message. Returns ({line, agent, type, mentions}, error).

    Cross-process locked, so the server, the CLI and append_hub_entry.py
    can all write at once. Keeps the file's own line endings.
    """
    path = game_path(game)
    if path is None:
        return None, "Unknown channel '%s'. GET /games lists them." % game
    game = os.path.basename(path)
    canon = store.resolve_agent(agent)
    if not canon:
        return None, "Unknown agent '%s'. Known: %s. Add agents in Settings > Agents." % (
            agent, ", ".join(store.agent_names()))
    a = store.get_agent(canon)
    if a and a["archived"]:
        return None, "Agent '%s' is archived. Restore it in Settings > Agents." % canon
    if not isinstance(text, str) or not text.replace("\r", "").strip():
        return None, "Empty message."
    clean = text.replace("\r", "").strip()
    limit = store.load_config()["settings"]["max_message_chars"]
    if len(clean) > limit:
        return None, "Message too long (%d chars, max %d). Split it, or put detail in the tree or a task." % (len(clean), limit)
    t = normalize_type(msg_type)
    if t == "":
        return None, "Unknown type '%s'. Use one of: %s." % (msg_type, ", ".join(POST_TYPES))
    recipients = []
    for name in (to or []):
        canon_to = "all" if str(name).lower() in ("all", "everyone") else store.resolve_agent(name)
        if not canon_to:
            return None, "Unknown recipient '%s'." % name
        if canon_to not in recipients:
            recipients.append(canon_to)
    # a body line that looks like "Name: ..." would forge a new message
    guarded = ["> " + ln if match_agent_line(ln) else ln for ln in clean.split("\n")]
    with store.FileLock(path):
        with open(path, "rb") as f:
            data = f.read()
        nl = b"\r\n" if data.count(b"\r\n") * 2 > data.count(b"\n") else b"\n"
        stripped = data.rstrip(b"\r\n")
        trailing = data[len(stripped):].count(b"\n")
        prefix = b"" if not stripped or trailing >= 2 else (nl if trailing == 1 else nl + nl)
        line_no = (data + prefix).count(b"\n") + 1
        body = ("%s: %s" % (canon, "\n".join(guarded))).encode("utf-8").replace(b"\n", nl) + nl
        with open(path, "ab") as f:
            f.write(prefix + body)
        store.append_meta(game, {"line": line_no, "ts": store.now_iso(), "agent": canon,
                                 "type": t, "to": recipients})
    mentions = extract_mentions(clean)
    for r in recipients:
        if r not in mentions:
            mentions.append(r)
    return {"line": line_no, "agent": canon, "type": t or classify_type(clean[:800]),
            "mentions": mentions}, ""


def append_agent_message(game, agent, text):
    """Back-compat wrapper (v2 API)."""
    res, err = append_message(game, agent, text)
    return (True, "") if res else (None, err)


def append_human_message(game, text):
    res, err = append_message(game, HUMAN, text)
    return (True, "") if res else (None, err)


# ---------------------------------------------------------------- project tree

TREE_MAX_CHARS = 60000
TREE_MAX_NODES = 300
TREE_MAX_DEPTH = 6
TREE_MAX_TITLE = 120
TREE_MAX_DETAIL = 600


def tree_path(game):
    path = game_path(game)
    return path + ".tree.json" if path else None


def blank_tree():
    return {"version": 0, "updated_at": "", "updated_by": "", "roots": []}


def load_tree(game):
    path = tree_path(game)
    if path is None:
        return None, None
    t = store.read_json(path, None)
    if not isinstance(t, dict) or not isinstance(t.get("roots"), list):
        t = blank_tree()
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
    canon = store.resolve_agent(agent)
    if not canon:
        return None, "Unknown agent. Valid: %s." % ", ".join(store.agent_names())
    t, path = load_tree(game)
    if t is None:
        return None, "Unknown channel."
    try:
        exp = int(expected_version)
    except (TypeError, ValueError):
        return None, "expected_version (int) is required - GET /tree first, then send back its version."
    roots, err = validate_tree_roots(roots)
    if err:
        return None, err
    with store.FileLock(path):  # version check + write must be atomic
        t, path = load_tree(game)
        if exp != int(t.get("version", 0)):
            return None, ("Version conflict: tree is at v%d, you sent expected_version=%s. "
                          "Re-fetch GET /tree and re-apply your change." % (t.get("version", 0), expected_version))
        t["roots"] = roots
        t["version"] = exp + 1
        t["updated_by"] = canon
        t["updated_at"] = store.now_iso()
        store.write_json(path, t)
    return t, ""


# ---------------------------------------------------------------- project lifecycle

def delete_game(game, confirm_understand, confirm_name, confirm_irreversible):
    """Confirmed project removal. Returns (payload, error).

    Files move to .trash/ (never unlinked). The default channel is protected.
    """
    if game_path(game) is None or not game:
        return None, "Unknown channel."
    if game == DEFAULT_CHANNEL_ID:
        return None, "The default channel (%s) cannot be deleted." % DEFAULT_CHANNEL_ID
    if confirm_understand is not True:
        return None, "Step 1/3 missing: confirm you understand the project leaves the app."
    if confirm_name != game:
        return None, "Step 2/3 missing: retype the exact channel file name."
    if confirm_irreversible != "DELETE":
        return None, "Step 3/3 missing: type DELETE to accept removal."
    trash = os.path.join(ROOT, ".trash")
    os.makedirs(trash, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
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
    pdir = store.project_dir(game)
    if os.path.isdir(pdir):
        os.replace(pdir, os.path.join(trash, stamp + "__" + os.path.basename(pdir) + ".aic"))
        moved.append(os.path.relpath(pdir, ROOT))
    return {"trashed": moved, "bytes_reclaimed": freed}, ""


TITLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _'\-]{0,60}$")


def valid_title(title):
    return isinstance(title, str) and bool(TITLE_RE.match(title.strip()))


def channel_template(game_id, title):
    q = quote(game_id)
    return (
        "# " + game_id + "\n"
        "# Shared channel where the AI agents and the human coordinate on '" + title + "'.\n"
        "# It is the team's chat log and handoff record, not game content.\n"
        "# Messages: 'AgentName: text' blocks, append-only. Never edit or delete others' lines.\n"
        "# Blank slate: never import memories, code or decisions from another project's channel.\n"
        "# Live view:  " + base_url() + "/?game=" + q + "\n"
        "# AGENTS:     GET " + base_url() + "/guide once, then every turn:\n"
        "#             GET " + base_url() + "/sync?game=" + q + "&agent=<YourName>&since=<cursor>\n"
    )


def create_game(title):
    """Create a new per-game channel file. Returns (game_id, created)."""
    clean = title.strip() if isinstance(title, str) else ""
    if not valid_title(clean):
        return None, False
    game_id = CHANNEL_PREFIX + " - " + clean + ".txt"
    for existing in channel_ids():  # case-insensitive filesystems
        if existing.lower() == game_id.lower():
            return existing, False
    path = os.path.join(ROOT, game_id)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(channel_template(game_id, clean))
    store.save_project(game_id, label=clean)
    return game_id, True



# ---------------------------------------------------------------- knowledge map

KMAP_CACHE = {}
KMAP_MAX_ENTITIES = 250
KMAP_FILE_RE = re.compile(r"\b(?:src/|horror_src/|dist/)[\w\-./]+\.\w+|\b[\w\-]+\.(?:luau|lua|py|js|md|txt|json)\b")
KMAP_TICK_RE = re.compile(r"`([^`\n]{2,60})`")
KMAP_CAMEL_RE = re.compile(r"\b[A-Z][A-Za-z0-9]{5,40}\b")
# capitalised words that start sentences and labels, not project concepts
KMAP_STOP = {"change", "notice", "changed", "verified", "verification", "status", "before", "after",
             "files", "checks", "tested", "because", "however", "should", "update", "updated", "fixed",
             "added", "removed", "created", "result", "results", "summary", "action", "required",
             "deliverables", "diagnostic", "report", "online", "standing", "follow", "please", "currently"}


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
    agents_lower = {a.lower() for a in store.agent_names()}
    for m in KMAP_CAMEL_RE.findall(nobracket):
        if m.lower() not in agents_lower and m.lower() not in KMAP_STOP:
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

    for a in store.agent_names():
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



# ---------------------------------------------------------------- brief-it + simplicity review

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



# ---------------------------------------------------------------- agent protocol

def guide_text(base=None):
    b = base or base_url()
    names = "/".join(a["name"] for a in store.roster(include_archived=False))
    return """AI INTERCONNECTION CENTER - AGENT PROTOCOL  (v{ver}; fetch once per session)
Several AI agents and one human work on the same project. This hub is how you
stay in sync without re-reading history. Base URL: {b}
Every endpoint takes ?game=<channel id> (GET /games lists them; omit = default channel).

1. EVERY TURN: ONE CALL
   GET /sync?game=<id>&agent=<You>&since=<cursor>
   -> new messages since your cursor (full if small, else digest + the ones addressed to you in full),
      your open tasks, paths other agents have claimed, team status, tree/rules changes, and a new cursor.
   First call: omit since. Keep the cursor it returns and send it next time. That is the whole read loop.

2. BEFORE EDITING FILES: CLAIM THEM
   POST /claim {{"game":..,"agent":"You","paths":["src/a.lua","src/ui/"],"note":"why","ttl_min":30}}
   409 = someone else holds an overlapping path (folders and globs overlap their contents).
   Pick other work or @mention the holder. Re-claiming the same paths renews the lease.
   POST /release {{"game":..,"agent":"You","all":true}} when done (or "id":"C3" / "paths":[..]).

3. AFTER EVERY CHANGE: CHANGE NOTICE
   POST /post {{"game":..,"agent":"You","type":"Change Notice","text":"WHAT: ..\\nFILES: ..\\nVERIFIED: how / NOT verified: what"}}
   Types: {types}. Address people with @Name (or "to":["Name"]); @Human asks the human; @all for everyone.
   Max {maxc} chars per message; longer detail belongs in the tree or a task.

4. TASKS
   GET  /tasks?game=..[&assignee=You][&status=todo,doing]
   POST /tasks {{"game","agent","title","detail","assignee","paths"}}          create (ids T1, T2..)
   POST /tasks/update {{"game","agent","id":"T3","status":"doing|review|done","assignee":..}}

5. SHARED CONTEXT
   GET  /tree?game=..&format=text ..... project tree: systems, files, invariants. Read it when /sync says it changed.
   POST /tree {{"agent","tree":[..],"expected_version":N}} after structural changes (409 = re-fetch first).
   GET  /project?game=.. .............. label + project rules (also included in /sync when they change).

6. DEEPER READS (only when you need history)
   GET /stats (~50 tok)  /digest (1 line/msg)  /brief (digest + last 3 full)
   GET /messages?tail=N | from_line=L&limit=1 | q=word | agent=A | type=T   (&format=json)
   GET /kmap/query?q=words  /kmap/path?a=&b=  /kmap/explain?node=   knowledge map over the history
   POST /brief-it {{"text"}} condense a draft   POST /simple {{"code"}} YAGNI review
   Never GET /messages with no params on a large channel: that is the full log.

7. RULES
   Identity: use exactly your roster name ({names}). Human is the human.
   Append-only: never edit or delete channel lines. One project per channel; never cross-post.
   Never edit paths another agent has claimed. Write for agents: facts, paths, numbers, no greetings.
   Status line (optional): POST /status {{"game","agent","doing":"running tests"}} shows on the human's dashboard.
   Access keys (only if the human turned them on): header X-AIC-Key: <your key>.
   MCP: python aic_mcp.py exposes all of this as tools (see the Connect tab in the dashboard).
""".format(ver=APP_VERSION, b=b, types=", ".join(POST_TYPES), names=names,
           maxc=store.load_config()["settings"]["max_message_chars"])


GUIDE_TEXT = guide_text()  # back-compat for channel_cli.py and older importers


def rules_signature(game):
    p = store.load_project(game)
    return p["updated_at"] if p["rules"] else ""


def build_sync(game, agent, since, max_chars=6000):
    """Everything an agent needs this turn, in one bounded payload."""
    items, _chars = load_items(game)
    cursor = items[-1]["line"] if items else 0
    first = since is None
    new = items[-5:] if first else [it for it in items if it["line"] > since]
    others = [it for it in new if it["agent"] != agent]
    # direct @mentions come back in full; broadcasts ([TO: ALL], @all) only when everything fits
    addressed = [it for it in others if agent in it["mentions"]] if not first else []
    full = sum(it["chars"] for it in others) <= max_chars and not first
    tasks = store.list_tasks(game)
    mine = [t for t in tasks if t["assignee"] == agent and t["status"] != "done"]
    unassigned = [t for t in tasks if not t["assignee"] and t["status"] == "todo"]
    claims = store.active_claims(game)
    others_claims = [c for c in claims if c["agent"] != agent]
    my_claims = [c for c in claims if c["agent"] == agent]
    tree, _ = load_tree(game)
    tv = int(tree.get("version", 0)) if tree else 0
    tree_seen = store.presence_get(game, agent, "tree_seen")
    rules_sig = rules_signature(game)
    rules_seen = store.presence_get(game, agent, "rules_seen")
    pres = store.presence(game)
    now = time.time()
    team = [{"agent": a, "ago_s": int(now - p["seen"]), "doing": p.get("doing", ""), "state": p.get("state", "")}
            for a, p in sorted(pres.items(), key=lambda kv: -kv[1]["seen"])
            if a != agent and now - p["seen"] < 1800]
    store.touch(game, agent, tree_seen=tv, rules_seen=rules_sig)
    project = store.load_project(game)

    def render(it, cap):
        body = it["text"]
        if len(body) > cap:
            body = body[:cap] + "\n...[cut, full: GET /messages?game=<id>&from_line=%d&limit=1]" % it["line"]
        return body

    return {
        "game": game, "agent": agent, "cursor": cursor, "since": since, "first_sync": first,
        "new_count": len(new), "own_count": len(new) - len(others),
        "messages": [dict(message_json(it, full=False),
                          text=render(it, 2500) if (full or it in addressed) else it["preview"][:180],
                          addressed=it in addressed, to_all="all" in it["mentions"],
                          full=full or it in addressed) for it in others],
        "your_tasks": mine, "unassigned_todo": len(unassigned),
        "claims_by_others": others_claims, "your_claims": my_claims,
        "tree": {"version": tv, "changed": tree_seen is not None and tree_seen != tv or (first and tv > 0),
                 "updated_by": tree.get("updated_by", "") if tree else ""},
        "rules": project["rules"] if project["rules"] and (first or rules_seen != rules_sig) else "",
        "team": team,
    }


def render_sync_text(s):
    g = s["game"]
    lines = ["SYNC %s as %s | cursor=%d (send since=%d next time)%s" % (
        channel_label(g), s["agent"], s["cursor"], s["cursor"],
        " | FIRST SYNC: showing last 5 as digest" if s["first_sync"] else "")]
    if s["rules"]:
        lines += ["", "PROJECT RULES (follow these):", s["rules"].strip()]
    if s["team"]:
        lines.append("TEAM: " + "; ".join("%s %s%s" % (t["agent"], _ago(t["ago_s"]), (" - " + t["doing"]) if t["doing"] else "")
                                         for t in s["team"]))
    if s["claims_by_others"]:
        lines.append("CLAIMED BY OTHERS (do not edit):")
        for c in s["claims_by_others"]:
            lines.append("  %s %s: %s%s (until %s)" % (c["id"], c["agent"], ", ".join(c["paths"]),
                                                    (' "%s"' % c["note"]) if c["note"] else "", c["expires_at"][11:16] + "Z"))
    if s["your_claims"]:
        lines.append("YOUR CLAIMS: " + "; ".join("%s %s" % (c["id"], ", ".join(c["paths"])) for c in s["your_claims"]))
    if s["your_tasks"]:
        lines.append("YOUR TASKS:")
        for t in s["your_tasks"]:
            lines.append("  %s [%s] %s%s" % (t["id"], t["status"], t["title"],
                                           (" (" + ", ".join(t["paths"]) + ")") if t["paths"] else ""))
    elif s["unassigned_todo"]:
        lines.append("TASKS: none assigned to you; %d unassigned todo (GET /tasks?status=todo)" % s["unassigned_todo"])
    if s["tree"]["changed"]:
        lines.append("TREE CHANGED: v%d by %s -> GET /tree?game=%s&format=text" % (
            s["tree"]["version"], s["tree"]["updated_by"] or "?", quote(g)))
    msgs = s["messages"]
    if not msgs:
        lines.append("NEW: nothing since L%s%s." % (s["since"], " (only your own posts)" if s["own_count"] else ""))
    else:
        lines.append("")
        lines.append("NEW (%d message%s%s):" % (len(msgs), "" if len(msgs) == 1 else "s",
                                               "" if all(m["full"] for m in msgs) else ", digest; addressed to you in full"))
        for m in msgs:
            mark = ">> " if m["addressed"] else ""
            if m["full"]:
                lines.append("%s--- L%d %s [%s] ---\n%s" % (mark, m["line"], m["agent"], m["type"], m["text"]))
            else:
                lines.append("%sL%d %s [%s] %s" % (mark, m["line"], m["agent"], m["type"], m["text"]))
    return "\n".join(lines)


def _ago(sec):
    if sec < 90:
        return "%ds ago" % sec
    if sec < 5400:
        return "%dm ago" % (sec // 60)
    return "%dh ago" % (sec // 3600)


def team_status(game):
    items, _ = load_items(game)
    pres = store.presence(game)
    claims = store.active_claims(game)
    tasks = store.list_tasks(game)
    counts, last = {}, {}
    for it in items:
        counts[it["agent"]] = counts.get(it["agent"], 0) + 1
        last[it["agent"]] = (it["line"], it["ts"])
    out = []
    for a in store.roster() + [{"name": HUMAN, "color": "", "role": "Project owner", "provider": "",
                                "model": "", "archived": False}]:
        n = a["name"]
        if a["archived"] and n not in counts:
            continue
        p = pres.get(n, {})
        out.append({
            "name": n, "color": a["color"], "role": a["role"], "archived": a["archived"],
            "hosted": store.is_hosted(a), "provider": a["provider"], "model": a["model"],
            "seen": p.get("seen"), "doing": p.get("doing", ""), "state": p.get("state", ""), "error": p.get("error", ""),
            "messages": counts.get(n, 0), "last_line": last.get(n, (0, None))[0], "last_ts": last.get(n, (0, None))[1],
            "claims": [c for c in claims if c["agent"] == n],
            "open_tasks": sum(1 for t in tasks if t["assignee"] == n and t["status"] != "done"),
        })
    return {"game": game, "now": time.time(), "agents": out}


def onboard_text(game, agent, base, fmt="rules"):
    """Copy-paste setup for one agent: rules file, MCP config, or CLI."""
    label = channel_label(game)
    q = quote(game)
    auth = store.load_config()["auth"]["enabled"]
    key_hdr = ' -H "X-AIC-Key: $AIC_KEY"' if auth else ""
    mcp_path = os.path.join(APP_DIR, "aic_mcp.py")
    py = "python"
    if fmt == "mcp":
        env = {"AIC_URL": base, "AIC_AGENT": agent, "AIC_GAME": game}
        if auth:
            env["AIC_KEY"] = "<your access key>"
        cfg = json.dumps({"mcpServers": {"aic": {"command": py, "args": [mcp_path], "env": env}}}, indent=2)
        envflags = " ".join('-e %s="%s"' % kv for kv in env.items())
        toml_env = ", ".join('%s = "%s"' % kv for kv in env.items())
        return """MCP server for {agent} (tools: aic_sync, aic_post, aic_claim, aic_release, aic_tasks, aic_task_update, aic_tree, aic_read, aic_search)

Claude Code:
  claude mcp add aic {envflags} -- {py} "{path}"

Codex CLI (~/.codex/config.toml):
  [mcp_servers.aic]
  command = "{py}"
  args = [{path_json}]
  env = {{ {toml_env} }}

Cursor (.cursor/mcp.json) / Cline / Windsurf / any MCP client:
{cfg}

Then add to the agent's rules: "Call aic_sync at the start of every turn. Claim paths with aic_claim before
editing. After each change call aic_post with type 'Change Notice'. Release claims when done."
""".format(agent=agent, envflags=envflags, py=py, path=mcp_path, path_json=json.dumps(mcp_path),
           toml_env=toml_env, cfg=cfg)
    if fmt == "cli":
        cli = os.path.join(APP_DIR, "channel_cli.py")
        g = game.replace('"', '\\"')
        return """Terminal agents (works the same on Windows, macOS, Linux; needs the hub running):
  set AIC_AGENT={agent}        (PowerShell: $env:AIC_AGENT="{agent}"   bash: export AIC_AGENT={agent})
  {py} "{cli}" sync --game "{g}"
  {py} "{cli}" claim --game "{g}" src/file.lua --note "why"
  {py} "{cli}" post --game "{g}" --type "Change Notice" --text "WHAT: ..  FILES: ..  VERIFIED: .."
  {py} "{cli}" release --game "{g}" --all
  {py} "{cli}" tasks --game "{g}" --mine
  {py} "{cli}" task-set --game "{g}" T3 --status review
The CLI stores the sync cursor for you (.aic/cursors). Raw HTTP works too:
  curl -s{key} "{base}/sync?game={q}&agent={agent}"
""".format(agent=agent, py=py, cli=cli, g=g, base=base, q=q, key=key_hdr)
    project = store.load_project(game)
    rules = ("\n### Project rules\n" + project["rules"].strip() + "\n") if project["rules"].strip() else ""
    keyline = ("\nSend the header `X-AIC-Key: <your access key>` on every request (the human gives you the key).\n"
               if auth else "")
    return """## Team coordination - AI Interconnection Center

You are **{agent}** on project **{label}**. Other AI agents work on this codebase at the same time;
a shared hub keeps you in sync and the human watches it live.
Hub: {base}    Project id: `{game}`{keyline}

### Every turn
1. **Sync first:** `GET {base}/sync?game={q}&agent={agent}&since=<cursor>`
   Returns new messages (the ones addressed to you in full), your tasks, files other agents have
   claimed, and a new `cursor`. Keep the cursor and send it next time (omit `since` the first time).
2. **Claim before you edit:** `POST {base}/claim` with
   `{{"game":"{game}","agent":"{agent}","paths":["path/to/file"],"note":"why","ttl_min":30}}`.
   HTTP 409 means another agent holds an overlapping path: work on something else or @mention them.
3. **Post a change notice after every change:** `POST {base}/post` with
   `{{"game":"{game}","agent":"{agent}","type":"Change Notice","text":"WHAT: ...\\nFILES: ...\\nVERIFIED: ..."}}`.
   Say exactly what you verified and what you did not.
4. **Finish:** release claims (`POST {base}/release` `{{"game":"{game}","agent":"{agent}","all":true}}`) and move
   your task (`POST {base}/tasks/update` `{{"game":"{game}","agent":"{agent}","id":"T3","status":"review"}}`).

### Rules
- Address teammates with @Name; ask the human with @Human; use type "Blocker" when you are stuck,
  "Question" for questions, "Handoff" when passing work on.
- When sync says the tree changed, read `GET {base}/tree?game={q}&format=text`; update the tree after
  structural changes (`POST /tree` with `expected_version`).
- Never edit a path another agent has claimed. Never edit or delete channel history.
- Write for other agents: facts, paths, numbers. No greetings, no recaps.
- Full protocol: `GET {base}/guide`.
{rules}""".format(agent=agent, label=label, base=base, game=game, q=q, keyline=keyline, rules=rules)


RULE_TEMPLATES = {
    "roblox": """- Server authority: game state, damage, currency, inventory and saves live in ServerScriptService. Never trust the client.
- Client code (StarterPlayerScripts / StarterCharacterScripts / StarterGui) does input, camera, UI and effects only.
- Shared modules go in ReplicatedStorage; server-only modules in ServerScriptService or ServerStorage, never in ReplicatedStorage.
- Every RemoteEvent / RemoteFunction handler validates its arguments (type, range, rate, ownership) on the server.
- No RemoteFunction calls from server to client (a client can hang the server).
- Rojo: edit files under src/ only, never the place file directly; keep default.project.json mappings in sync with new folders.
- DataStore: wrap calls in pcall, retry with backoff, version the save schema; never write per-frame.
- Report in every change notice: which side (server/client/shared) each changed script runs on.""",
    "godot": """- One owner per scene file (.tscn); claim the scene before editing it - merge conflicts in scenes are expensive.
- Autoloads hold global state; list any new autoload in the change notice.
- Signals over direct node paths across scenes; no get_node("../../..") chains.
- Exported builds must boot to the first scene: after every change, run the project headless or launch the export once.
- Keep resources (.tres) small and referenced, not duplicated; note any new input actions in project.godot.""",
    "web": """- API contract changes (routes, payloads, DB schema) need a Decision message before code lands.
- Each change notice lists the commands run (tests, lint, typecheck) and their result.
- No secrets in the repo; config through environment variables.
- Frontend and backend owners claim their folders; shared types live in one place.""",
}


# ---------------------------------------------------------------- hosted agents

_hosted_locks = {}
_hosted_guard = threading.Lock()


def hosted_prompt(game, agent, trigger=None, prompt_text="", requested_by=HUMAN):
    label = channel_label(game)
    project = store.load_project(game)
    limit = min(store.load_config()["settings"]["max_message_chars"], 6000)
    tree, _ = load_tree(game)
    tree_txt = render_tree_text(tree["roots"], 8000) if tree and tree.get("roots") else "(no project tree yet)"
    items, chars = load_items(game)
    brief = build_brief(items, chars, recent=6, budget_chars=14000)["text"] if items else "(channel is empty)"
    tasks = [t for t in store.list_tasks(game) if t["status"] != "done"]
    task_txt = "\n".join("%s [%s] %s -> %s" % (t["id"], t["status"], t["title"], t["assignee"] or "unassigned")
                         for t in tasks[:40]) or "(none)"
    system = (
        "You are %s%s, one of several AI agents working with a human on the project \"%s\". "
        "The team coordinates through a shared chat channel (the AI Interconnection Center) and you are replying inside it. "
        "You cannot edit files or run code from here: you answer, review, plan and decide. "
        "When work needs to be done in the codebase, say which agent should do it and @mention them. "
        "Write for other agents: facts first, exact names, paths and numbers, no greetings, no recap of the question. "
        "Keep the reply under %d characters. Do not start the reply with your own name."
        % (agent["name"], (", " + agent["role"]) if agent["role"] else "", label, limit))
    if project["rules"].strip():
        system += "\n\nProject rules every agent follows:\n" + project["rules"].strip()
    if agent["system"].strip():
        system += "\n\n" + agent["system"].strip()
    user = ("PROJECT TREE (shared context):\n%s\n\nOPEN TASKS:\n%s\n\nCHANNEL (older messages as a one-line digest, newest in full):\n%s\n\n"
            % (tree_txt, task_txt, brief))
    if trigger:
        user += "You were addressed in L%d by %s:\n%s\n\nWrite your reply." % (trigger["line"], trigger["agent"], trigger["text"])
    else:
        user += "Request from %s:\n%s\n\nWrite your reply." % (requested_by, prompt_text)
    return system, user, limit


def run_hosted(game, name, trigger=None, prompt_text="", requested_by=HUMAN):
    """Ask a hosted agent and post its reply. Returns (post_result, error)."""
    agent = store.get_agent(name)
    if not store.is_hosted(agent):
        return None, "%s is not a hosted agent (set a provider and model in Settings > Agents)." % name
    with _hosted_guard:
        lock = _hosted_locks.setdefault((game, agent["name"]), threading.Lock())
    with lock:  # one reply at a time per agent and project
        store.touch(game, agent["name"], state="thinking", error="",
                    doing="replying to L%d" % trigger["line"] if trigger else "answering %s" % requested_by)
        try:
            prov = llm.resolve(agent["provider"], store.load_config()["providers"])
            system, user, limit = hosted_prompt(game, agent, trigger, prompt_text, requested_by)
            reply = llm.complete(prov, agent["model"], system, user)
        except llm.LLMError as e:
            store.touch(game, agent["name"], state="error", error=str(e)[:300], doing="")
            return None, str(e)
        reply = re.sub(r"^\s*\**%s\**\s*:\s*" % re.escape(agent["name"]), "", reply, flags=re.I)
        if len(reply) > limit:
            reply = reply[:limit - 60].rstrip() + "\n...[reply cut at %d chars]" % (limit - 60)
        to = [trigger["agent"]] if trigger else [requested_by]
        res, err = append_message(game, agent["name"], reply, to=to)
        store.touch(game, agent["name"], state="error" if err else "", error=err or "", doing="")
        return res, err


def trigger_hosted(game, author, line, text, mentions):
    """Hosted agents @mentioned by anyone who is not hosted reply in the background.

    Replies from hosted agents never trigger other hosted agents, so two of
    them cannot loop.
    """
    if store.is_hosted(store.get_agent(author)):
        return []
    names = []
    for m in mentions:
        a = store.get_agent(m) if m != "all" else None
        if store.is_hosted(a) and a["name"] != author:
            names.append(a["name"])
    trigger = {"line": line, "agent": author, "text": text}
    for n in names:
        threading.Thread(target=run_hosted, args=(game, n, trigger), daemon=True).start()
    return names


def post(game, actor, text, msg_type=None, to=None):
    res, err = append_message(game, actor, text, msg_type=msg_type, to=to)
    if not res:
        return None, err
    game = os.path.basename(game_path(game))
    store.touch(game, res["agent"])
    res["triggered"] = trigger_hosted(game, res["agent"], res["line"], text.strip(), res["mentions"])
    return res, ""


def task_event(game, actor, task, changes, created=False):
    """One short audit line in the channel for task changes that matter."""
    if created:
        text = "[TASK %s created] %s" % (task["id"], task["title"])
        if task["assignee"]:
            text += " -> @%s" % task["assignee"]
    elif changes.get("deleted"):
        text = "[TASK %s deleted] %s" % (task["id"], task["title"])
    else:
        parts = []
        if "status" in changes:
            parts.append(changes["status"])
        if "assignee" in changes:
            parts.append(("-> @" + changes["assignee"]) if changes["assignee"] else "unassigned")
        if not parts:
            return
        text = "[TASK %s %s] %s" % (task["id"], " ".join(parts), task["title"])
    append_message(game, actor, text, msg_type="Status")


# ---------------------------------------------------------------- HTTP

MAX_BODY = 2 * 1024 * 1024
STATIC = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js",
          "/app.css": "app.css", "/icon.svg": "icon.svg"}
CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
          ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
PUBLIC = set(STATIC) | {"/health", "/guide"}
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
GET_ROUTES, POST_ROUTES = {}, {}


def route(method, path):
    def deco(fn):
        (GET_ROUTES if method == "GET" else POST_ROUTES)[path] = fn
        return fn
    return deco


class HTTPError(Exception):
    def __init__(self, status, msg, **extra):
        super().__init__(msg)
        self.status, self.msg, self.extra = status, msg, extra


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AIC/" + APP_VERSION

    def log_message(self, *args):
        pass

    # ---- responses
    def send_bytes(self, body, ctype, status=200, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_json(self, obj, status=200):
        self.send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                        "application/json; charset=utf-8", status)

    def send_text(self, text, status=200):
        self.send_bytes(text.encode("utf-8"), "text/plain; charset=utf-8", status)

    # ---- request helpers
    def qp(self, name, default=None):
        v = self.query.get(name)
        return v[0] if v else default

    def qint(self, name, default, lo, hi):
        try:
            return max(lo, min(int(self.qp(name, default)), hi))
        except (TypeError, ValueError):
            return default

    def body(self):
        if hasattr(self, "_body"):
            return self._body
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > MAX_BODY:
            raise HTTPError(413, "Request body too large (max 2 MB).")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            raise HTTPError(400, "Body must be JSON.")
        if not isinstance(data, dict):
            raise HTTPError(400, "Body must be a JSON object.")
        self._body = data
        return data

    def game(self, required=True):
        """Channel id from ?game= or the JSON body, validated."""
        g = self.qp("game")
        if g is None and self.command == "POST":
            g = self.body().get("game")
        path = game_path(g)
        if path is None:
            if required:
                raise HTTPError(404, "Unknown channel '%s'. GET /games lists them." % g)
            return None
        return os.path.basename(path)

    def base(self):
        host = self.headers.get("Host") or "127.0.0.1:%d" % PORT
        return "http://" + host

    # ---- security
    def host_ok(self):
        if HOST not in ("127.0.0.1", "localhost", "::1"):
            return True  # LAN mode: access keys are the protection
        host = (self.headers.get("Host") or "").strip().lower()
        name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
        return name in LOOPBACK or name in set(os.environ.get("AIC_ALLOWED_HOSTS", "").lower().split(","))

    def origin_ok(self):
        """Blocks other websites from POSTing to the hub (CSRF)."""
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True  # not a browser (curl, agents, CLI)
        if origin == "null":
            return False
        host = (self.headers.get("Host") or "").lower()
        return urlparse(origin).netloc.lower() == host

    def identify(self):
        key = self.headers.get("X-AIC-Key") or ""
        auth = self.headers.get("Authorization") or ""
        if not key and auth.lower().startswith("bearer "):
            key = auth[7:].strip()
        if not key and self.parts.path == "/events":
            key = self.qp("key", "")  # EventSource cannot set headers
        return store.identify_key(key)

    def actor(self, name, allow_human=True):
        """Canonical name of whoever is acting, after auth rules."""
        if self.auth_on and self.ident != "admin":
            if name and store.resolve_agent(name) != self.ident:
                raise HTTPError(403, "This access key belongs to %s, not %s." % (self.ident, name))
            return self.ident
        canon = store.resolve_agent(name or "")
        if not canon:
            raise HTTPError(400, "Unknown agent '%s'. Known: %s." % (name, ", ".join(store.agent_names())))
        if canon == HUMAN and not allow_human:
            raise HTTPError(403, "Human is reserved for the person using the dashboard.")
        return canon

    def require_admin(self):
        if self.auth_on and self.ident != "admin":
            raise HTTPError(403, "Needs the admin key.")

    # ---- dispatch
    def do_HEAD(self):
        self.dispatch("GET")

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        self.parts = urlparse(self.path)
        self.query = parse_qs(self.parts.query)
        path = self.parts.path.rstrip("/") or "/"
        try:
            if not self.host_ok():
                raise HTTPError(403, "Host not allowed (DNS-rebinding guard). Set AIC_ALLOWED_HOSTS to add one.")
            if method == "POST" and not self.origin_ok():
                raise HTTPError(403, "Cross-site request blocked.")
            if method == "GET" and path in STATIC:
                return self.static(STATIC[path])
            routes = GET_ROUTES if method == "GET" else POST_ROUTES
            fn = routes.get(path)
            if fn is None and path.startswith("/kmap/") and method == "GET":
                fn = routes.get("/kmap")
            if fn is None:
                raise HTTPError(404, "No such endpoint: %s %s. See GET /guide." % (method, path))
            self.auth_on = store.load_config()["auth"]["enabled"]
            self.ident = self.identify()
            if self.auth_on and path not in PUBLIC and not self.ident:
                raise HTTPError(401, "Access key required (header X-AIC-Key).")
            fn(self)
        except HTTPError as e:
            self.send_json(dict({"ok": False, "error": e.msg}, **e.extra), e.status)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except TimeoutError as e:
            self.send_json({"ok": False, "error": str(e)}, 503)
        except Exception as e:  # never leave a client hanging on a bug
            traceback.print_exc()
            try:
                self.send_json({"ok": False, "error": "Server error: %s" % e}, 500)
            except OSError:
                pass

    def static(self, name):
        path = os.path.join(WEB_DIR, name)
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            raise HTTPError(404, "Missing web/%s next to channel_viewer.py." % name)
        self.send_bytes(body, CTYPES[os.path.splitext(name)[1]])


# ---- read endpoints

@route("GET", "/health")
def h_health(h):
    h.send_json({"ok": True, "app": "aic", "port": PORT, "version": APP_VERSION, "games": len(channel_ids())})


@route("GET", "/guide")
def h_guide(h):
    text = guide_text(h.base())
    if h.qp("format") == "json":
        h.send_json({"est_tokens": est_tokens(len(text)), "chars": len(text), "text": text})
    else:
        h.send_text(text)


@route("GET", "/games")
def h_games(h):
    h.send_json(list_games())


@route("GET", "/stats")
def h_stats(h):
    items, chars = load_items(h.game())
    h.send_json(build_stats(items, chars))


@route("GET", "/digest")
def h_digest(h):
    game = h.game()
    items, _ = load_items(game)
    per_msg = h.qint("per_msg", 180, 40, 500)
    max_msgs = h.qint("max_msgs", 0, 0, 100000)
    items = filter_items(items, agent=h.qp("agent"), q=h.qp("q"), msg_type=h.qp("type"))
    if max_msgs > 0:
        items = items[-max_msgs:]
    digest = build_digest(items, per_msg=per_msg)
    if h.qp("format", "json") == "text":
        lines = ["# DIGEST %s (%d messages, one line each)" % (game, len(digest)), ""]
        for d in digest:
            tags = (" [" + "|".join(d["tags"][:3]) + "]") if d["tags"] else ""
            lines.append("L%d [%s][%s]%s %s" % (d["line"], d["agent"], d["type"], tags, d["snippet"]))
        h.send_text("\n".join(lines))
    else:
        h.send_json({"game": game, "count": len(digest), "per_msg": per_msg,
                     "est_tokens": est_tokens(sum(len(d["snippet"]) for d in digest)), "digest": digest})


@route("GET", "/brief")
def h_brief(h):
    game = h.game()
    items, chars = load_items(game)
    brief = build_brief(items, chars, recent=h.qint("recent", 3, 1, 10),
                        budget_chars=h.qint("budget_chars", 16000, 2000, 60000))
    if h.qp("format", "json") == "text":
        h.send_text(brief["text"])
    else:
        h.send_json(dict({"game": game}, **brief))


@route("GET", "/messages")
def h_messages(h):
    game = h.game()
    fmt = h.qp("format", "text")
    keys = ("tail", "limit", "from_line", "before_line", "max_chars", "q", "agent", "type")
    if fmt != "json" and not any(k in h.query for k in keys):
        text, _ = read_channel_text(game)  # legacy full dump
        return h.send_text(text)
    items, _ = load_items(game)
    items = filter_items(items, agent=h.qp("agent"), q=h.qp("q"), msg_type=h.qp("type"))
    fl = h.qint("from_line", 0, 0, 10 ** 9)
    if fl:
        items = [it for it in items if it["line"] >= fl]
    bl = h.qint("before_line", 0, 0, 10 ** 9)
    if bl:
        items = [it for it in items if it["line"] < bl]
    total = len(items)
    limit = h.qint("limit", 0, 0, 100000)
    if limit:
        items = items[:limit]  # oldest-first window (use with from_line)
    tail = h.qint("tail", 0, 0, 100000)
    if tail:
        items = items[-tail:]
    if fmt == "json":
        return h.send_json({"game": game, "count": len(items), "total": total,
                            "est_tokens": est_tokens(sum(it["chars"] for it in items)),
                            "messages": [message_json(it) for it in items]})
    body = "\n\n".join("%s: %s" % (it["agent"], it["text"]) for it in items)
    mc = h.qint("max_chars", 0, 500, 200000) if "max_chars" in h.query else 0
    if mc and len(body) > mc:
        body = "...[truncated oldest-first, %d chars shown]...\n\n" % mc + body[-mc:]
    h.send_text(body)


@route("GET", "/tree")
def h_tree(h):
    game = h.game()
    t, _ = load_tree(game)
    nodes = sum(1 for _ in _walk_tree(t.get("roots", [])))
    if h.qp("format", "json") == "text":
        mc = h.qint("max_chars", 12000, 1000, 60000)
        body = ("# TREE %s (v%d, %d nodes, by %s) - project context\n\n"
                % (channel_label(game), t.get("version", 0), nodes, t.get("updated_by") or "-")) + \
            (render_tree_text(t.get("roots", []), max_chars=mc) or "(empty - POST /tree to create it)")
        return h.send_text(body)
    raw = json.dumps(t.get("roots", []), ensure_ascii=False)
    h.send_json({"game": game, "version": t.get("version", 0), "updated_at": t.get("updated_at", ""),
                 "updated_by": t.get("updated_by", ""), "nodes": nodes, "est_tokens": est_tokens(len(raw)),
                 "tree": t.get("roots", [])})


@route("GET", "/kmap")
def h_kmap(h):
    game = h.game()
    sub = h.parts.path[len("/kmap"):].strip("/")
    km = build_kmap(game)
    if sub == "":
        return h.send_json({"game": game, "messages": km["messages"], "nodes": km["node_count"],
                            "edges": km["edge_count"], "est_tokens": km["est_tokens"],
                            "gods": km["gods"], "communities": km["communities"]})
    if sub == "query":
        return h.send_json(dict({"game": game, "q": h.qp("q", "")}, **kmap_query(km, h.qp("q", ""))))
    if sub == "path":
        res = kmap_path(km, h.qp("a", ""), h.qp("b", ""))
    elif sub == "explain":
        res = kmap_explain(km, h.qp("node", ""))
    else:
        raise HTTPError(404, "Unknown kmap view '%s'." % sub)
    h.send_json(res, 400 if "error" in res else 200)


@route("GET", "/download")
def h_download(h):
    zp = os.path.join(APP_DIR, "dist", "AI-Interconnection-Center-%s.zip" % APP_VERSION)
    try:
        with open(zp, "rb") as f:
            body = f.read()
    except OSError:
        raise HTTPError(404, "No package built yet. Run: python build_package.py")
    h.send_bytes(body, "application/zip", extra={
        "Content-Disposition": 'attachment; filename="AI-Interconnection-Center-%s.zip"' % APP_VERSION})


@route("GET", "/sync")
def h_sync(h):
    game = h.game()
    agent = h.actor(h.qp("agent"), allow_human=True)
    since = h.qp("since")
    try:
        since = int(since) if since not in (None, "") else None
    except ValueError:
        raise HTTPError(400, "since must be the integer cursor from your last sync.")
    s = build_sync(game, agent, since, max_chars=h.qint("max_chars", 6000, 1000, 40000))
    h.send_json(s) if h.qp("format") == "json" else h.send_text(render_sync_text(s))


@route("GET", "/tasks")
def h_tasks(h):
    game = h.game()
    tasks = store.list_tasks(game, status=h.qp("status"), assignee=h.qp("assignee"))
    if h.qp("format") == "text":
        lines = ["%s [%s] %s -> %s%s" % (t["id"], t["status"], t["title"], t["assignee"] or "unassigned",
                                         (" (" + ", ".join(t["paths"]) + ")") if t["paths"] else "") for t in tasks]
        return h.send_text("\n".join(lines) or "(no tasks)")
    h.send_json({"game": game, "tasks": tasks, "statuses": list(store.TASK_STATUSES)})


@route("GET", "/claims")
def h_claims(h):
    game = h.game()
    h.send_json({"game": game, "claims": store.active_claims(game)})


@route("GET", "/team")
def h_team(h):
    h.send_json(team_status(h.game()))


@route("GET", "/project")
def h_project(h):
    game = h.game()
    p = store.load_project(game)
    h.send_json({"game": game, "label": channel_label(game), "rules": p["rules"], "updated_at": p["updated_at"],
                 "templates": RULE_TEMPLATES, "default": game == DEFAULT_CHANNEL_ID})


@route("GET", "/onboard")
def h_onboard(h):
    game = h.game()
    agent = store.resolve_agent(h.qp("agent", "")) or (store.roster(False) or [{"name": "Claude"}])[0]["name"]
    fmt = h.qp("format", "rules")
    if fmt not in ("rules", "mcp", "cli"):
        raise HTTPError(400, "format must be rules, mcp or cli.")
    h.send_text(onboard_text(game, agent, h.base(), fmt))


@route("GET", "/api/config")
def h_config_get(h):
    cfg = store.public_config()
    cfg_prov = store.load_config()["providers"]
    cfg["presets"] = [dict(p, key_source=llm.key_source(p["id"], cfg_prov)) for p in llm.PRESETS]
    cfg["types"] = POST_TYPES
    cfg["version"] = APP_VERSION
    cfg["you"] = h.ident or ("admin" if not h.auth_on else None)
    h.send_json(cfg)


def snapshot(game):
    path = game_path(game)
    sig = {}
    for k, p in (("messages", path), ("tree", path + ".tree.json" if path else None),
                 ("tasks", store.project_file(game, "tasks.json")),
                 ("claims", store.project_file(game, "claims.json")),
                 ("project", store.project_file(game, "project.json")),
                 ("config", store.CONFIG_PATH)):
        try:
            st = os.stat(p) if p else None
            sig[k] = (st.st_mtime_ns, st.st_size) if st else None
        except OSError:
            sig[k] = None
    sig["team"] = store.presence_version()
    sig["games"] = tuple(channel_ids())
    return sig


@route("GET", "/events")
def h_events(h):
    """Server-sent events: tells the dashboard what changed, the client refetches."""
    game = h.game()
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    h.send_header("Cache-Control", "no-store")
    h.send_header("X-Accel-Buffering", "no")
    h.end_headers()
    last = snapshot(game)
    started = time.time()
    h.wfile.write(b"retry: 2000\nevent: hello\ndata: {}\n\n")
    h.wfile.flush()
    idle = 0
    while time.time() - started < 1800:  # the browser reconnects on its own
        time.sleep(0.75)
        cur = snapshot(game)
        changed = [k for k in cur if cur[k] != last.get(k)]
        last = cur
        if changed:
            h.wfile.write(("event: change\ndata: %s\n\n" % json.dumps(changed)).encode())
            idle = 0
        else:
            idle += 1
            if idle >= 20:
                h.wfile.write(b": ping\n\n")
                idle = 0
        h.wfile.flush()


# ---- write endpoints

def _post_common(h, allow_human):
    game = h.game()
    b = h.body()
    agent = h.actor(b.get("agent"), allow_human=allow_human)
    to = b.get("to")
    if isinstance(to, str):
        to = [x for x in re.split(r"[,\s]+", to) if x]
    res, err = post(game, agent, b.get("text", ""), msg_type=b.get("type"), to=to)
    if not res:
        raise HTTPError(400, err)
    h.send_json(dict({"ok": True, "error": ""}, **res))


@route("POST", "/post")
def h_post(h):
    # the dashboard marks itself so only the human composer posts as Human
    _post_common(h, allow_human=h.headers.get("X-AIC-Client") == "web")


@route("POST", "/agent_append")
def h_agent_append(h):
    _post_common(h, allow_human=False)


@route("POST", "/messages")
def h_human_post(h):
    b = h.body()
    b.setdefault("agent", HUMAN)
    if store.resolve_agent(b["agent"]) != HUMAN:
        raise HTTPError(400, "POST /messages is the human composer; agents use POST /post.")
    _post_common(h, allow_human=True)


@route("POST", "/ask")
def h_ask(h):
    """Ask a hosted agent directly; waits for its reply (which is also posted)."""
    game = h.game()
    b = h.body()
    requested_by = h.actor(b.get("from") or HUMAN, allow_human=True)
    name = store.resolve_agent(b.get("agent", ""))
    prompt = (b.get("prompt") or "").strip()
    if not name or not prompt:
        raise HTTPError(400, "Send {agent, prompt}.")
    res, err = run_hosted(game, name, prompt_text=prompt, requested_by=requested_by)
    if not res:
        raise HTTPError(502, err)
    items, _ = load_items(game)
    reply = next((it["text"] for it in reversed(items) if it["line"] == res["line"]), "")
    h.send_json(dict({"ok": True, "reply": reply}, **res))


@route("POST", "/status")
def h_status(h):
    game = h.game()
    b = h.body()
    agent = h.actor(b.get("agent"))
    store.touch(game, agent, doing=str(b.get("doing", ""))[:140])
    h.send_json({"ok": True})


@route("POST", "/claim")
def h_claim(h):
    game = h.game()
    b = h.body()
    agent = h.actor(b.get("agent"))
    force = bool(b.get("force"))
    if force and agent != HUMAN:
        raise HTTPError(403, "Only the human can force a claim over someone else's.")
    c, conflicts, err = store.claim(game, agent, b.get("paths") or b.get("path"), b.get("ttl_min", 30),
                                    b.get("note", ""), b.get("task", ""), force=force)
    if err:
        raise HTTPError(400, err)
    if conflicts and not c:
        who = sorted({x["agent"] for x in conflicts})
        raise HTTPError(409, "Claimed by %s: %s. Work on something else or @mention them." % (
            ", ".join(who), "; ".join("%s %s" % (x["id"], ", ".join(x["paths"])) for x in conflicts)),
            conflicts=conflicts)
    store.touch(game, agent, doing=c["note"] or ("editing " + ", ".join(c["paths"][:2])))
    h.send_json({"ok": True, "claim": c, "overridden": conflicts})


@route("POST", "/release")
def h_release(h):
    game = h.game()
    b = h.body()
    agent = h.actor(b.get("agent"))
    gone, err = store.release(game, agent, claim_id=b.get("id"), paths=b.get("paths"),
                              release_all=bool(b.get("all")), privileged=agent == HUMAN)
    if gone is None:
        raise HTTPError(403, err)
    if not gone:
        raise HTTPError(404, err)
    h.send_json({"ok": True, "released": gone})


@route("POST", "/tasks")
def h_task_create(h):
    game = h.game()
    b = h.body()
    agent = h.actor(b.get("agent"))
    t, err = store.create_task(game, agent, b)
    if err:
        raise HTTPError(400, err)
    if not b.get("quiet"):
        task_event(game, agent, t, {}, created=True)
    store.touch(game, agent)
    h.send_json({"ok": True, "task": t})


@route("POST", "/tasks/update")
def h_task_update(h):
    game = h.game()
    b = h.body()
    agent = h.actor(b.get("agent"))
    fields = {k: v for k, v in b.items() if k in ("title", "detail", "status", "assignee", "paths", "delete")}
    t, changes, err = store.update_task(game, agent, b.get("id", ""), fields)
    if err:
        raise HTTPError(404 if err.startswith("Unknown task") else 400, err)
    if not b.get("quiet"):
        task_event(game, agent, t, changes)
    store.touch(game, agent)
    h.send_json({"ok": True, "task": t, "changes": changes})


@route("POST", "/tree")
def h_tree_post(h):
    game = h.game()
    b = h.body()
    agent = h.actor(b.get("agent"))
    t, err = save_tree(game, agent, b.get("tree", b.get("roots")), b.get("expected_version"))
    if not t:
        raise HTTPError(409 if err.startswith("Version conflict") else 400, err)
    h.send_json({"ok": True, "error": "", "version": t["version"]})


@route("POST", "/project")
def h_project_post(h):
    game = h.game()
    b = h.body()
    agent = h.actor(b.get("agent") or HUMAN)
    if agent != HUMAN:
        raise HTTPError(403, "Project label and rules are set by the human.")
    p, err = store.save_project(game, label=b.get("label"), rules=b.get("rules"))
    if err:
        raise HTTPError(400, err)
    h.send_json({"ok": True, "project": p})


@route("POST", "/brief-it")
def h_brief_it(h):
    b = h.body()
    try:
        mc = max(200, min(int(b.get("max_chars", 2000)), 8000))
    except (TypeError, ValueError):
        mc = 2000
    h.send_json(dict({"ok": True}, **brief_text(b.get("text", ""), max_chars=mc)))


@route("POST", "/simple")
def h_simple(h):
    code = h.body().get("code", "")
    if not isinstance(code, str) or len(code) > 60000:
        raise HTTPError(400, "Send code as a string (max 60000 chars).")
    h.send_json(dict({"ok": True}, **simplicity_check(code)))


@route("POST", "/new_game")
def h_new_game(h):
    h.actor(h.body().get("agent") or HUMAN)
    game_id, created = create_game(h.body().get("title", ""))
    if game_id is None:
        raise HTTPError(400, "Title must start with a letter or digit, up to 61 chars of letters/digits/spaces/_/-/'.")
    h.send_json({"ok": True, "id": game_id, "created": created})


@route("POST", "/delete_game")
def h_delete_game(h):
    h.require_admin()
    b = h.body()
    data, error = delete_game(b.get("game"), b.get("confirm_understand"), b.get("confirm_name"),
                              b.get("confirm_irreversible"))
    if not data:
        raise HTTPError(404 if error == "Unknown channel." else 403 if error.startswith("The default") else 400, error)
    h.send_json(dict({"ok": True, "error": ""}, **data))


@route("POST", "/api/config")
def h_config_post(h):
    h.require_admin()
    cfg, err, issued = store.update_config(h.body())
    if err:
        raise HTTPError(400, err)
    h.send_json({"ok": True, "config": cfg, "issued": issued})


@route("POST", "/api/keys")
def h_keys(h):
    h.require_admin()
    key, err = store.issue_key(h.body().get("target", ""))
    if err:
        raise HTTPError(400, err)
    h.send_json({"ok": True, "key": key})


@route("POST", "/api/providers/test")
def h_provider_test(h):
    h.require_admin()
    b = h.body()
    pid = b.get("provider", "")
    model = (b.get("model") or "").strip()
    if not model:
        raise HTTPError(400, "Pick a model to test with.")
    try:
        prov = llm.resolve(pid, store.load_config()["providers"])
        t0 = time.time()
        reply = llm.complete(prov, model, "You are a connectivity check.", "Reply with the single word OK.", timeout=60)
    except llm.LLMError as e:
        raise HTTPError(502, str(e))
    h.send_json({"ok": True, "reply": reply[:200], "ms": int((time.time() - t0) * 1000)})


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    os.makedirs(store.STATE_DIR, exist_ok=True)
    if not os.path.exists(CHANNEL_FILE):  # fresh install: start with one channel
        with open(CHANNEL_FILE, "w", encoding="utf-8", newline="\n") as f:
            f.write(channel_template(DEFAULT_CHANNEL_ID, "Main"))
    try:
        httpd = ThreadedTCPServer((HOST, PORT), Handler)
    except OSError as e:
        print("Could not listen on %s:%d (%s). Is the hub already running? Try AIC_PORT=19844." % (HOST, PORT, e))
        return 1
    cfg = store.load_config()
    print("AI Interconnection Center %s at %s" % (APP_VERSION, base_url()))
    print("Agents: GET /guide once, then GET /sync?game=<id>&agent=<Name>&since=<cursor> every turn")
    if HOST not in ("127.0.0.1", "localhost", "::1") and not cfg["auth"]["enabled"]:
        print("WARNING: listening on %s without access keys - anyone on your network can post, claim and"
              " read. Turn on access keys in Settings > Security." % HOST)
    if cfg["auth"]["enabled"]:
        print("Access keys are ON. The admin key is in %s" % store.CONFIG_PATH)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
