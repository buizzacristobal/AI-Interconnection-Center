"""State that lives next to the channel files.

  .aic/config.json            agents, AI provider keys, access keys   (secret, gitignored)
  .aic/projects/<id>/         per-project data you can commit:
      project.json            display label + project rules every agent gets
      tasks.json              task board
      claims.json             who is editing which paths right now
      meta.jsonl              per-message timestamp / explicit type / recipients
  .aic/locks/                 cross-process lock files                 (gitignored)

Presence (who is online, what they are doing) is in memory only.
Standard library only.
"""

import datetime
import fnmatch
import hashlib
import json
import os
import re
import secrets
import threading
import time

HOME = os.path.abspath(os.environ.get("AIC_HOME") or os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(HOME, ".aic")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
PROJECTS_DIR = os.path.join(STATE_DIR, "projects")
LOCK_DIR = os.path.join(STATE_DIR, "locks")

HUMAN = "Human"
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{1,23}$")
# Words that open continuation lines inside real messages ("FILES: ...").
# An agent with one of these names would split every such message in two.
RESERVED_NAMES = {
    "human", "system", "all", "everyone", "what", "why", "how", "files", "file",
    "note", "notes", "status", "verify", "verified", "verification", "plan", "todo",
    "fix", "to", "from", "date", "scope", "heads-up", "warning", "error", "result",
    "results", "summary", "changed", "tested", "known", "problem", "next", "done",
}
DEFAULT_AGENTS = [
    ("Antigravity", "#3fb6f0"), ("OpenCode", "#2fbf86"), ("Cursor", "#9b7bf0"),
    ("Cline", "#d9a514"), ("Claude", "#d97a3a"), ("Codex", "#e8579a"),
    ("Qwen", "#14a9c9"), ("Deepseek", "#e15656"), ("Kimi", "#8a94a6"),
    ("ZCode", "#6fae12"),
]
TASK_STATUSES = ("todo", "doing", "review", "done")
MAX_MESSAGE_CHARS = 8000


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- locking

_thread_locks = {}
_thread_locks_guard = threading.Lock()


class FileLock:
    """Thread + process lock for one path (lock file under .aic/locks).

    os.open(O_CREAT|O_EXCL) is atomic on every OS Python runs on, so this
    works for the server, the CLI, and append_hub_entry.py at once. A lock
    older than `stale` seconds is assumed to belong to a crashed writer.
    """

    def __init__(self, path, timeout=15.0, stale=20.0):
        key = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:16]
        self.lockfile = os.path.join(LOCK_DIR, key + ".lock")
        self.timeout, self.stale = timeout, stale
        with _thread_locks_guard:
            self.tlock = _thread_locks.setdefault(key, threading.RLock())
        self.depth = 0

    def __enter__(self):
        self.tlock.acquire()
        self.depth += 1
        if self.depth > 1:
            return self
        os.makedirs(LOCK_DIR, exist_ok=True)
        deadline = time.time() + self.timeout
        while True:
            try:
                fd = os.open(self.lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.lockfile) > self.stale:
                        os.remove(self.lockfile)
                        continue
                except OSError:
                    continue
                if time.time() > deadline:
                    self.depth -= 1
                    self.tlock.release()
                    raise TimeoutError("Timed out waiting for lock on a channel file.")
                time.sleep(0.02)

    def __exit__(self, *exc):
        self.depth -= 1
        if self.depth == 0:
            try:
                os.remove(self.lockfile)
            except OSError:
                pass
        self.tlock.release()


def read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        return default


def write_json(path, obj, private=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    if private:
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader has the file open
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


# ---------------------------------------------------------------- config

_cfg_cache = {"mtime": None, "cfg": None}
_cfg_lock = threading.RLock()


def _default_config():
    return {
        "agents": [{"name": n, "color": c, "role": "", "provider": "", "model": "",
                    "system": "", "aliases": [], "archived": False, "key": ""}
                   for n, c in DEFAULT_AGENTS],
        "providers": {},
        "auth": {"enabled": False, "admin_key": ""},
        "settings": {"max_message_chars": MAX_MESSAGE_CHARS},
    }


def _normalize(cfg):
    base = _default_config()
    if not isinstance(cfg, dict):
        return base
    agents = []
    seen = set()
    for a in cfg.get("agents") or []:
        if not isinstance(a, dict) or not NAME_RE.match(str(a.get("name", ""))):
            continue
        if a["name"].lower() in seen:
            continue
        seen.add(a["name"].lower())
        agents.append({
            "name": a["name"],
            "color": a.get("color") if re.match(r"^#[0-9a-fA-F]{6}$", str(a.get("color", ""))) else "#8a94a6",
            "role": str(a.get("role", ""))[:120],
            "provider": str(a.get("provider", "")),
            "model": str(a.get("model", ""))[:120],
            "system": str(a.get("system", ""))[:4000],
            "aliases": [x for x in (a.get("aliases") or []) if isinstance(x, str) and NAME_RE.match(x)][:8],
            "archived": bool(a.get("archived")),
            "key": str(a.get("key", "")),
        })
    base["agents"] = agents or base["agents"]
    if isinstance(cfg.get("providers"), dict):
        base["providers"] = {k: {"api_key": str(v.get("api_key", "")), "base_url": str(v.get("base_url", ""))}
                             for k, v in cfg["providers"].items() if isinstance(v, dict)}
    if isinstance(cfg.get("auth"), dict):
        base["auth"] = {"enabled": bool(cfg["auth"].get("enabled")),
                        "admin_key": str(cfg["auth"].get("admin_key", ""))}
    if isinstance(cfg.get("settings"), dict):
        try:
            mm = int(cfg["settings"].get("max_message_chars", MAX_MESSAGE_CHARS))
        except (TypeError, ValueError):
            mm = MAX_MESSAGE_CHARS
        base["settings"]["max_message_chars"] = max(500, min(mm, 20000))
    return base


def load_config():
    with _cfg_lock:
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
        except OSError:
            mtime = None
        if _cfg_cache["cfg"] is not None and _cfg_cache["mtime"] == mtime:
            return _cfg_cache["cfg"]
        cfg = _normalize(read_json(CONFIG_PATH, None)) if mtime else _default_config()
        _cfg_cache.update(mtime=mtime, cfg=cfg)
        return cfg


def save_config(cfg):
    with _cfg_lock:
        cfg = _normalize(cfg)
        write_json(CONFIG_PATH, cfg, private=True)
        _cfg_cache.update(mtime=os.path.getmtime(CONFIG_PATH), cfg=cfg)
        return cfg


def config_mtime():
    try:
        return os.path.getmtime(CONFIG_PATH)
    except OSError:
        return 0


def mask(secret):
    if not secret:
        return ""
    return "..." + secret[-4:] if len(secret) > 8 else "set"


# ---------------------------------------------------------------- roster

def roster(include_archived=True):
    agents = load_config()["agents"]
    return [a for a in agents if include_archived or not a["archived"]]


def agent_names(include_human=True):
    names = [a["name"] for a in roster()]
    return names + [HUMAN] if include_human else names


def _alias_map():
    cfg = load_config()
    if _alias_cache.get("cfg") is not cfg:
        m = {HUMAN.lower(): HUMAN}
        for a in cfg["agents"]:
            m[a["name"].lower()] = a["name"]
            for al in a["aliases"]:
                m.setdefault(al.lower(), a["name"])
        _alias_cache.update(cfg=cfg, map=m)
    return _alias_cache["map"]


_alias_cache = {}


def resolve_agent(name):
    """Canonical agent name for any casing or alias ('DEEPSEEK' -> 'Deepseek')."""
    if not isinstance(name, str):
        return None
    return _alias_map().get(name.strip().lower())


def get_agent(name):
    canon = resolve_agent(name)
    for a in roster():
        if a["name"] == canon:
            return a
    return None


def is_hosted(agent):
    """A hosted agent is answered by this server through an AI provider."""
    return bool(agent and agent.get("provider") and agent.get("model") and not agent.get("archived"))


def validate_new_name(name, existing):
    if not isinstance(name, str) or not NAME_RE.match(name):
        return "Agent names: 2-24 letters, digits, - or _, starting with a letter."
    if name.lower() in RESERVED_NAMES:
        return "'%s' is reserved (it starts ordinary lines like '%s: ...' inside messages)." % (name, name)
    if name.lower() in {n.lower() for n in existing}:
        return "An agent called '%s' already exists." % name
    return ""


def public_config():
    """Config safe to send to a browser: every secret is masked."""
    cfg = load_config()
    out = {
        "agents": [{k: v for k, v in a.items() if k != "key"} | {"has_key": bool(a["key"]), "key_hint": mask(a["key"])}
                   for a in cfg["agents"]],
        "providers": {k: {"base_url": v["base_url"], "has_key": bool(v["api_key"]), "key_hint": mask(v["api_key"])}
                      for k, v in cfg["providers"].items()},
        "auth": {"enabled": cfg["auth"]["enabled"], "has_admin_key": bool(cfg["auth"]["admin_key"])},
        "settings": dict(cfg["settings"]),
    }
    return out


def update_config(patch):
    """Merge a browser patch. Secrets: '' keeps the stored value, '__clear__' removes it."""
    with _cfg_lock:
        cfg = json.loads(json.dumps(load_config()))
        issued = {}
        if "agents" in patch:
            if not isinstance(patch["agents"], list):
                return None, "agents must be a list.", issued
            old = {a["name"]: a for a in cfg["agents"]}
            new_list = []
            for a in patch["agents"]:
                if not isinstance(a, dict):
                    continue
                name = a.get("name", "")
                if name in old:
                    merged = dict(old.pop(name))
                else:
                    err = validate_new_name(name, [x["name"] for x in new_list] + list(old))
                    if err:
                        return None, err, issued
                    merged = {"name": name, "color": "#8a94a6", "role": "", "provider": "", "model": "",
                              "system": "", "aliases": [], "archived": False, "key": ""}
                for k in ("color", "role", "provider", "model", "system", "archived"):
                    if k in a:
                        merged[k] = a[k]
                if "aliases" in a and isinstance(a["aliases"], list):
                    merged["aliases"] = a["aliases"]
                new_list.append(merged)
            # agents missing from the patch are archived, never dropped: their
            # name must stay in the parser or their history stops parsing
            for a in old.values():
                a["archived"] = True
                new_list.append(a)
            cfg["agents"] = new_list
        if "providers" in patch and isinstance(patch["providers"], dict):
            for pid, p in patch["providers"].items():
                if not re.match(r"^[a-z0-9_\-]{2,32}$", pid) or not isinstance(p, dict):
                    continue
                if p.get("remove"):
                    cfg["providers"].pop(pid, None)
                    continue
                cur = cfg["providers"].setdefault(pid, {"api_key": "", "base_url": ""})
                key = p.get("api_key", "")
                if key == "__clear__":
                    cur["api_key"] = ""
                elif isinstance(key, str) and key.strip():
                    cur["api_key"] = key.strip()
                if "base_url" in p and isinstance(p["base_url"], str):
                    url = p["base_url"].strip().rstrip("/")
                    if url and not re.match(r"^https?://", url):
                        return None, "Base URL must start with http:// or https://", issued
                    cur["base_url"] = url
        if "auth" in patch and isinstance(patch["auth"], dict):
            if "enabled" in patch["auth"]:
                cfg["auth"]["enabled"] = bool(patch["auth"]["enabled"])
                if cfg["auth"]["enabled"] and not cfg["auth"]["admin_key"]:
                    cfg["auth"]["admin_key"] = "aic_admin_" + secrets.token_urlsafe(24)
                    issued["admin_key"] = cfg["auth"]["admin_key"]
        if "settings" in patch and isinstance(patch["settings"], dict):
            cfg["settings"].update({k: v for k, v in patch["settings"].items() if k in ("max_message_chars",)})
        save_config(cfg)
        return public_config(), "", issued


def issue_key(target):
    """New access key for an agent (or 'admin'). Returned once, stored in config."""
    with _cfg_lock:
        cfg = json.loads(json.dumps(load_config()))
        if target == "admin":
            cfg["auth"]["admin_key"] = "aic_admin_" + secrets.token_urlsafe(24)
            key = cfg["auth"]["admin_key"]
        else:
            canon = resolve_agent(target)
            agent = next((a for a in cfg["agents"] if a["name"] == canon), None)
            if not agent:
                return None, "Unknown agent."
            agent["key"] = "aic_" + agent["name"].lower() + "_" + secrets.token_urlsafe(18)
            key = agent["key"]
        save_config(cfg)
        return key, ""


def identify_key(key):
    """'admin', an agent name, or None."""
    if not key:
        return None
    cfg = load_config()
    if cfg["auth"]["admin_key"] and secrets.compare_digest(key, cfg["auth"]["admin_key"]):
        return "admin"
    for a in cfg["agents"]:
        if a["key"] and not a["archived"] and secrets.compare_digest(key, a["key"]):
            return a["name"]
    return None


# ---------------------------------------------------------------- per-project files

def project_dir(game):
    base = game[:-4] if game.lower().endswith(".txt") else game
    return os.path.join(PROJECTS_DIR, base)


def project_file(game, name):
    return os.path.join(project_dir(game), name)


def load_project(game):
    p = read_json(project_file(game, "project.json"), {})
    return {"label": str(p.get("label", "")) if isinstance(p, dict) else "",
            "rules": str(p.get("rules", "")) if isinstance(p, dict) else "",
            "updated_at": str(p.get("updated_at", "")) if isinstance(p, dict) else ""}


def save_project(game, label=None, rules=None):
    path = project_file(game, "project.json")
    with FileLock(path):
        p = load_project(game)
        if label is not None:
            label = label.strip()
            if len(label) > 60:
                return None, "Label too long (max 60)."
            p["label"] = label
        if rules is not None:
            if len(rules) > 6000:
                return None, "Rules too long (max 6000 chars)."
            p["rules"] = rules.replace("\r", "")
        p["updated_at"] = now_iso()
        write_json(path, p)
        return p, ""


# ---- message metadata (timestamp, explicit type, recipients) keyed by line

_meta_cache = {}


def append_meta(game, entry):
    path = project_file(game, "meta.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_meta(game):
    path = project_file(game, "meta.jsonl")
    try:
        st = os.stat(path)
    except OSError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    hit = _meta_cache.get(path)
    if hit and hit[0] == sig:
        return hit[1]
    out = {}
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            try:
                e = json.loads(ln)
                out[int(e["line"])] = e
            except (ValueError, KeyError, TypeError):
                continue
    _meta_cache[path] = (sig, out)
    return out


# ---- tasks

def _tasks_doc(game):
    d = read_json(project_file(game, "tasks.json"), {})
    if not isinstance(d, dict) or not isinstance(d.get("tasks"), list):
        d = {"next": 1, "tasks": []}
    d.setdefault("next", len(d["tasks"]) + 1)
    return d


def list_tasks(game, status=None, assignee=None):
    tasks = _tasks_doc(game)["tasks"]
    if status:
        want = {s.strip().lower() for s in status.split(",")}
        tasks = [t for t in tasks if t["status"] in want]
    if assignee:
        canon = resolve_agent(assignee) or assignee
        tasks = [t for t in tasks if t.get("assignee") == canon]
    return tasks


def _clean_task_fields(fields, partial):
    out = {}
    if "title" in fields or not partial:
        title = str(fields.get("title", "")).strip()
        if not title:
            return None, "Task needs a title."
        if len(title) > 160:
            return None, "Title too long (max 160)."
        out["title"] = title
    if "detail" in fields:
        detail = str(fields.get("detail") or "")
        if len(detail) > 4000:
            return None, "Detail too long (max 4000)."
        out["detail"] = detail.replace("\r", "")
    if "status" in fields:
        st = str(fields.get("status") or "todo").lower()
        if st not in TASK_STATUSES:
            return None, "Status must be one of: %s." % ", ".join(TASK_STATUSES)
        out["status"] = st
    if "assignee" in fields:
        a = fields.get("assignee") or ""
        canon = resolve_agent(a) if a else ""
        if a and not canon:
            return None, "Unknown assignee '%s'." % a
        out["assignee"] = canon or ""
    if "paths" in fields:
        paths = fields.get("paths") or []
        if isinstance(paths, str):
            paths = [p for p in re.split(r"[\n,]+", paths)]
        out["paths"] = [str(p).strip() for p in paths if str(p).strip()][:30]
    return out, ""


def create_task(game, actor, fields):
    clean, err = _clean_task_fields(fields, partial=False)
    if err:
        return None, err
    path = project_file(game, "tasks.json")
    with FileLock(path):
        d = _tasks_doc(game)
        t = {"id": "T%d" % d["next"], "title": clean["title"], "detail": clean.get("detail", ""),
             "status": clean.get("status", "todo"), "assignee": clean.get("assignee", ""),
             "paths": clean.get("paths", []), "created_by": actor, "created_at": now_iso(),
             "updated_by": actor, "updated_at": now_iso()}
        d["next"] += 1
        d["tasks"].append(t)
        write_json(path, d)
        return t, ""


def update_task(game, actor, tid, fields):
    path = project_file(game, "tasks.json")
    with FileLock(path):
        d = _tasks_doc(game)
        t = next((x for x in d["tasks"] if x["id"].lower() == str(tid).lower()), None)
        if not t:
            return None, None, "Unknown task '%s'." % tid
        if fields.get("delete"):
            d["tasks"].remove(t)
            write_json(path, d)
            return t, {"deleted": True}, ""
        clean, err = _clean_task_fields(fields, partial=True)
        if err:
            return None, None, err
        changes = {k: v for k, v in clean.items() if t.get(k) != v}
        t.update(clean)
        t["updated_by"], t["updated_at"] = actor, now_iso()
        write_json(path, d)
        return t, changes, ""


# ---- claims (soft locks on paths, with expiry)

def norm_path(p):
    p = str(p).strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    p = re.sub(r"/{2,}", "/", p).rstrip("/")
    return p


def paths_overlap(a, b):
    a, b = norm_path(a).lower(), norm_path(b).lower()
    if not a or not b:
        return False
    if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
        return True
    if any(c in a for c in "*?[") and fnmatch.fnmatch(b, a):
        return True
    if any(c in b for c in "*?[") and fnmatch.fnmatch(a, b):
        return True
    return False


def _claims_doc(game):
    d = read_json(project_file(game, "claims.json"), {})
    if not isinstance(d, dict) or not isinstance(d.get("claims"), list):
        d = {"next": 1, "claims": []}
    d.setdefault("next", len(d["claims"]) + 1)
    return d


def _prune(d):
    t = time.time()
    d["claims"] = [c for c in d["claims"] if parse_iso(c.get("expires_at")) > t]
    return d


def active_claims(game):
    return _prune(_claims_doc(game))["claims"]


def claim(game, agent, paths, ttl_min=30, note="", task="", force=False):
    """Returns (claim, conflicts, error). conflicts non-empty means refused."""
    if isinstance(paths, str):
        paths = [paths]
    paths = [norm_path(p) for p in (paths or []) if norm_path(p)]
    if not paths:
        return None, [], "Give at least one path to claim."
    if len(paths) > 40:
        return None, [], "Too many paths in one claim (max 40)."
    try:
        ttl = max(1, min(int(ttl_min), 480))
    except (TypeError, ValueError):
        ttl = 30
    path = project_file(game, "claims.json")
    with FileLock(path):
        d = _prune(_claims_doc(game))
        conflicts = [c for c in d["claims"] if c["agent"] != agent
                     and any(paths_overlap(p, q) for p in paths for q in c["paths"])]
        if conflicts and not force:
            return None, conflicts, ""
        if force:
            d["claims"] = [c for c in d["claims"] if c not in conflicts]
        expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=ttl)
        exp = expires.strftime("%Y-%m-%dT%H:%M:%SZ")
        same = next((c for c in d["claims"] if c["agent"] == agent
                     and sorted(c["paths"]) == sorted(paths)), None)
        if same:  # re-claiming the same paths renews the lease
            same.update(expires_at=exp, note=str(note)[:200] or same.get("note", ""), task=str(task or same.get("task", "")))
            c = same
        else:
            c = {"id": "C%d" % d["next"], "agent": agent, "paths": paths, "note": str(note)[:200],
                 "task": str(task or "")[:20], "created_at": now_iso(), "expires_at": exp}
            d["next"] += 1
            d["claims"].append(c)
        write_json(path, d)
        return c, conflicts if force else [], ""


def release(game, actor, claim_id=None, paths=None, release_all=False, privileged=False):
    path = project_file(game, "claims.json")
    with FileLock(path):
        d = _prune(_claims_doc(game))
        if isinstance(paths, str):
            paths = [paths]
        keep, gone = [], []
        for c in d["claims"]:
            mine = c["agent"] == actor or privileged
            hit = False
            if claim_id and c["id"].lower() == str(claim_id).lower():
                hit = True
            elif release_all and c["agent"] == actor:
                hit = True
            elif paths and any(paths_overlap(p, q) for p in paths for q in c["paths"]) and c["agent"] == actor:
                hit = True
            if hit and not mine:
                return None, "Claim %s belongs to %s. Only they (or the human) can release it." % (c["id"], c["agent"])
            (gone if hit else keep).append(c)
        if not gone:
            return [], "Nothing matched to release."
        d["claims"] = keep
        write_json(path, d)
        return gone, ""


# ---------------------------------------------------------------- presence (memory only)

_presence = {}
_presence_lock = threading.Lock()
_presence_version = [0]


def touch(game, agent, **fields):
    if not game or not agent:
        return
    with _presence_lock:
        p = _presence.setdefault((game, agent), {"seen": 0.0, "doing": "", "state": "", "error": ""})
        p["seen"] = time.time()
        for k, v in fields.items():
            if v is not None:
                p[k] = v
        _presence_version[0] += 1


def presence(game):
    with _presence_lock:
        return {a: dict(p) for (g, a), p in _presence.items() if g == game}


def presence_get(game, agent, field, default=None):
    with _presence_lock:
        return _presence.get((game, agent), {}).get(field, default)


def presence_version():
    return _presence_version[0]
