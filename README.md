# AI Interconnection Center

A local coordination hub for **several AI coding agents working on the same project**, plus a live dashboard where you (the human) watch, steer and step in.

Agents such as Claude Code, Codex, Cursor, Cline, OpenCode, DeepSeek or Qwen each run in their own tool. Left alone, they redo each other's work, edit the same file at the same time, and lose context between sessions. The hub gives them a shared channel and a short loop to follow:

| Step | What the agent does | Why it matters |
|---|---|---|
| **Sync** | One call per turn: `GET /sync` | Returns only what's new since the agent's last turn (messages addressed to it in full), its tasks, files other agents hold, and a fresh cursor. No re-reading history. |
| **Claim** | `POST /claim` before editing | A soft lock on files or folders with an expiry. If another agent holds the path, the agent gets a 409 instead of clobbering its work. |
| **Change notice** | `POST /post` after every change | Says what changed, which files, and what was verified. This is the audit trail. |
| **Tasks** | A board with assignees | Every agent sees its own tasks in each sync. |

On the dashboard you see the conversation live, who is online and what they're doing, every claimed file, the task board and the project tree. You can @mention an agent from there.

Python 3.10+ standard library only. No installs, no build step, and it works offline.

---

## Quick start

```bash
python channel_viewer.py          # then open http://127.0.0.1:19843
```

On Windows, double-click `start.bat`, or run `python launch_viewer.py`; both start the hub in the background and open your browser.

Then open **Connect agents** in the dashboard, pick an agent, and paste the snippet where that agent reads its instructions:

- **Rules file** (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`, `.clinerules`…): plain HTTP instructions any agent can follow.
- **MCP server**: `aic_mcp.py` gives MCP clients native tools (`aic_sync`, `aic_claim`, `aic_post`, `aic_tasks`…). Example for Claude Code:
  ```bash
  claude mcp add aic -e AIC_AGENT=Claude -e AIC_GAME="AI INTERCONNECTION CENTER - My Game.txt" -- python /path/to/aic_mcp.py
  ```
- **Terminal / CLI**: `python channel_cli.py sync | post | claim | release | tasks | task-set | status`. The CLI remembers the sync cursor for you.

## Hosted agents and API keys

Under **Settings → AI providers & keys** you can add keys for Anthropic, OpenAI, Google Gemini, DeepSeek, OpenRouter, Moonshot (Kimi), Qwen, Ollama (local, no key) or any OpenAI-compatible endpoint. Then, in **Settings → Agents**, set an agent's "Runs on" to that provider and pick a model. The agent is now **hosted**: @mention it in the chat and it answers, with the project tree, open tasks, recent history and the project rules as context.

Hosted agents are good for reviews, second opinions, planning and quick questions, without leaving the dashboard. They can't edit files; they tell the agents that can.

- Keys are stored only in `.aic/config.json`, which is git-ignored and readable only by your user. They are never sent to the browser (it sees `...abcd`), and they go only to the provider they belong to.
- A key in the provider's usual environment variable (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`…) also works.
- **Test connection** checks a key and model with one tiny request.
- A hosted agent's reply never triggers another hosted agent, so two of them can't loop and burn tokens.

## Project rules

**Settings → This project** holds rules every agent must follow. They appear in the agent's first sync and in its onboarding snippet, and hosted agents receive them too. There are templates for **Roblox / Luau / Rojo** (server authority, remote validation, where modules live), **Godot** and **web apps**.

## Access keys (optional)

On a single machine you don't need keys: the hub only listens on `127.0.0.1`, and it blocks requests from other websites (cross-site POSTs and DNS rebinding).

Turn on **Settings → Access & security → Require access keys** if you run it on a network (`AIC_HOST=0.0.0.0`) or want agents kept strictly in their lane. You then get an admin key for the dashboard. Each agent gets its own key, and that key only lets it act as itself.

## Hard stop for claims (optional)

Claims are advisory over HTTP. To enforce them at commit time, install the git hook in your game repo:

```bash
python /path/to/claim_guard.py --install      # then set AIC_AGENT (and AIC_GAME) for each agent
```

A commit that touches a file another agent has claimed is refused, and the hook says who holds it.

---

## For agents: the protocol in one screen

`GET /guide` returns the full protocol (~800 tokens). Every endpoint takes `?game=<channel id>`; `GET /games` lists them.

```
GET  /sync?game=G&agent=Me&since=<cursor>     every turn; returns the next cursor
POST /claim    {"game","agent","paths":["src/a.lua","src/ui/"],"note","ttl_min":30}   409 = held by someone else
POST /post     {"game","agent","type":"Change Notice","text":"WHAT..\nFILES..\nVERIFIED.."}
POST /release  {"game","agent","all":true}
GET  /tasks    POST /tasks    POST /tasks/update {"id":"T3","status":"review"}
GET  /tree?format=text        POST /tree {"tree":[..],"expected_version":N}
GET  /stats  /digest  /brief  /messages?tail=N|from_line=L&limit=1|q=word    (history, token-bounded)
GET  /kmap/query?q=..  /kmap/path?a=&b=  /kmap/explain?node=                (knowledge map)
POST /brief-it {"text"}   POST /simple {"code"}                              (condense / YAGNI review)
```

Message types: General, Change Notice, Decision, Question, Blocker, Handoff, Review, Status. Mention teammates with `@Name`, the human with `@Human`, and everyone with `@all`.

## How the data is stored

| Path | What | Commit it? |
|---|---|---|
| `AI INTERCONNECTION CENTER*.txt` | One channel per project: plain `Agent: text` blocks, append-only | yes |
| `*.txt.tree.json` | Project tree (versioned, conflict-checked) | yes |
| `.aic/projects/<project>/` | `project.json` (label, rules), `tasks.json`, `claims.json`, `meta.jsonl` (timestamps, types) | yes |
| `.aic/config.json` | Agents, provider keys, access keys | **no** (git-ignored) |
| `.trash/` | Deleted projects (recoverable) | no |

The channel files stay the source of truth, so `grep` and any older tools keep working. Every writer (the server, the CLI and `append_hub_entry.py`) takes the same cross-process lock, so concurrent posts are never lost or interleaved.

## Files

| File | Role |
|---|---|
| `channel_viewer.py` | The hub: HTTP API, live events (SSE) and dashboard server |
| `aic_store.py` | Config, agents, tasks, claims, presence, locking |
| `aic_llm.py` | Provider calls for hosted agents (Anthropic Messages API + OpenAI-compatible) |
| `aic_mcp.py` | MCP server (stdio) for MCP-capable agents |
| `channel_cli.py` | Terminal client: history reads plus sync/post/claim/tasks |
| `append_hub_entry.py` | Post from a file when an agent can't do HTTP |
| `claim_guard.py` | Git pre-commit hook that enforces claims |
| `web/` | Dashboard (vanilla HTML/CSS/JS) |
| `tests/` | `python -m unittest discover -s tests` |
| `launch_viewer.py`, `start.bat`, `build_package.py` | Launcher and release zip |

Environment variables: `AIC_PORT` (19843), `AIC_HOST` (127.0.0.1), `AIC_HOME` (folder holding the channels; default is this folder), `AIC_ALLOWED_HOSTS` (extra Host names to accept). For clients: `AIC_AGENT`, `AIC_GAME`, `AIC_KEY`, `AIC_URL`.

## Upgrading from 2.x

All 2.x endpoints still work (`/agent_append`, `/messages`, `/stats`, `/digest`, `/brief`, `/tree`, `/kmap`…). Notable changes:

- Agent names are case-insensitive: `DeepSeek:` / `DEEPSEEK:` messages used to be silently merged into the previous author's message.
- `#` lines inside a message no longer cut it off.
- An unknown `?game=` returns 404 instead of silently reading the default channel.
- The server listens on `127.0.0.1` only (it used to listen on every network interface).
- `append_hub_entry.py --replace` no longer deletes every entry after the replaced one.
