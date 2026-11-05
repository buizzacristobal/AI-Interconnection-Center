"""End-to-end tests. Run:  python -m unittest discover -s tests -v

Everything runs in a throwaway AIC_HOME with a real server on a free port.
"""

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HOME = tempfile.mkdtemp(prefix="aic-test-")
os.environ["AIC_HOME"] = HOME
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

GAME = "AI INTERCONNECTION CENTER - Test Game.txt"
LEGACY = (
    "# AI INTERCONNECTION CENTER - Test Game.txt\r\n# header line\r\n"
    "OpenCode: first message\r\nFILES: a.lua\r\n# a python comment inside the message\r\n\r\n"
    "DeepSeek: shouted name still counts\r\n\r\n"
    "DEEPSEEK: [TO: ALL / Cline] all caps too\r\n"
)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = free_port()
os.environ["AIC_PORT"] = str(PORT)
with open(os.path.join(HOME, GAME), "wb") as f:
    f.write(LEGACY.encode())

import aic_store as store  # noqa: E402
import channel_viewer as cv  # noqa: E402

BASE = "http://127.0.0.1:%d" % PORT


def req(path, body=None, headers=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(r, timeout=20) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if resp.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def q(game=GAME):
    return "game=" + urllib.request.quote(game)


class FakeProvider(http.server.BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeProvider.calls.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        out = json.dumps({"choices": [{"message": {"content": "Reviewer: looks right, @Codex ship it"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def setUpModule():
    global SERVER, PROVIDER
    SERVER = cv.ThreadedTCPServer(("127.0.0.1", PORT), cv.Handler)
    threading.Thread(target=SERVER.serve_forever, daemon=True).start()
    PROVIDER = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    threading.Thread(target=PROVIDER.serve_forever, daemon=True).start()


def tearDownModule():
    SERVER.shutdown()
    PROVIDER.shutdown()
    shutil.rmtree(HOME, ignore_errors=True)


class T1Parsing(unittest.TestCase):
    def test_case_insensitive_agents_and_comment_lines(self):
        items, _ = cv.load_items(GAME)
        self.assertEqual([it["agent"] for it in items[:3]], ["OpenCode", "Deepseek", "Deepseek"])
        self.assertIn("FILES: a.lua", items[0]["text"])  # not a new message
        self.assertIn("# a python comment", items[0]["text"])  # '#' no longer truncates
        self.assertIn("all", items[2]["mentions"])
        self.assertIn("Cline", items[2]["mentions"])

    def test_unknown_game_does_not_fall_back(self):
        self.assertIsNone(cv.game_path("nope.txt"))
        self.assertIsNone(cv.game_path("../secret.txt"))


class T2Writing(unittest.TestCase):
    def test_append_keeps_crlf_and_line_numbers_match(self):
        res, err = cv.append_message(GAME, "codex", "hello\nClaude: forged line", msg_type="question", to=["cline"])
        self.assertEqual(err, "")
        with open(os.path.join(HOME, GAME), "rb") as f:
            raw = f.read()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))  # still pure CRLF
        items, _ = cv.load_items(GAME)
        it = next(i for i in items if i["line"] == res["line"])
        self.assertEqual(it["agent"], "Codex")
        self.assertEqual(it["type"], "Question")
        self.assertIn("> Claude: forged line", it["text"])
        self.assertTrue(it["ts"])
        self.assertIn("Cline", it["mentions"])

    def test_concurrent_posts_are_not_lost(self):
        before = len(cv.load_items(GAME)[0])

        def worker(n):
            for i in range(5):
                cv.append_message(GAME, "Qwen", "msg %d-%d" % (n, i))
        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        items, _ = cv.load_items(GAME)
        self.assertEqual(len(items), before + 40)
        lines = [it["line"] for it in items if it["agent"] == "Qwen"]
        self.assertEqual(len(set(lines)), 40)
        self.assertTrue(all(it["ts"] for it in items if it["agent"] == "Qwen"))  # meta lines line up

    def test_append_hub_entry_replace_keeps_later_entries(self):
        tool = os.path.join(ROOT, "append_hub_entry.py")
        env = dict(os.environ)
        run = lambda *a: subprocess.run([sys.executable, tool] + list(a), env=env, capture_output=True, text=True)  # noqa: E731
        self.assertEqual(run("--agent", "Kimi", "--text", "x").returncode, 2)  # no --hub: refuses
        self.assertEqual(run("--game", GAME, "--agent", "Kimi", "--text", "STEP 1 draft").returncode, 0)
        self.assertEqual(run("--game", GAME, "--agent", "Kimi", "--text", "STEP 1").returncode, 0)  # prefix != duplicate
        self.assertEqual(run("--game", GAME, "--agent", "Kimi", "--text", "STEP 1").returncode, 4)
        cv.append_message(GAME, "Cline", "later entry must survive")
        entry = os.path.join(HOME, "entry.txt")
        with open(entry, "w") as f:
            f.write("Kimi: STEP 1\nrevised body")
        r = run(entry, "--hub", GAME, "--replace")
        self.assertEqual(r.returncode, 0, r.stdout)
        with open(os.path.join(HOME, GAME), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("revised body", text)
        self.assertIn("later entry must survive", text)


class T3ClaimsTasks(unittest.TestCase):
    def test_overlap_rules(self):
        self.assertTrue(store.paths_overlap("src/ui", "src/ui/hud.lua"))
        self.assertTrue(store.paths_overlap("./src\\a.lua", "src/a.lua"))
        self.assertTrue(store.paths_overlap("src/*.lua", "src/a.lua"))
        self.assertFalse(store.paths_overlap("src/ui", "src/uix.lua"))

    def test_claim_conflict_renew_release(self):
        c, conf, _ = store.claim(GAME, "Cursor", ["src/game/"], note="refactor")
        self.assertTrue(c and not conf)
        c2, conf2, _ = store.claim(GAME, "Codex", ["src/game/save.gd"])
        self.assertIsNone(c2)
        self.assertEqual(conf2[0]["agent"], "Cursor")
        again, _, _ = store.claim(GAME, "Cursor", ["src/game/"], ttl_min=60)
        self.assertEqual(again["id"], c["id"])  # renewed, not duplicated
        gone, err = store.release(GAME, "Codex", claim_id=c["id"])
        self.assertIsNone(gone)
        gone, err = store.release(GAME, "Human", claim_id=c["id"], privileged=True)
        self.assertEqual(len(gone), 1)

    def test_task_validation(self):
        t, err = store.create_task(GAME, "Human", {"title": "Fix boot", "assignee": "deepseek"})
        self.assertEqual(t["assignee"], "Deepseek")
        _, _, err = store.update_task(GAME, "Human", t["id"], {"status": "nonsense"})
        self.assertIn("Status", err)
        _, _, err = store.update_task(GAME, "Human", t["id"], {"assignee": "Nobody"})
        self.assertIn("Unknown assignee", err)


class T4Http(unittest.TestCase):
    def test_sync_cursor_flow(self):
        code, s = req("/sync?%s&agent=Codex&format=json" % q())
        self.assertEqual(code, 200)
        self.assertTrue(s["first_sync"])
        cursor = s["cursor"]
        req("/post", {"game": GAME, "agent": "Cline", "text": "@Codex please verify", "type": "Question"})
        code, s = req("/sync?%s&agent=Codex&since=%d&format=json" % (q(), cursor))
        self.assertEqual(s["new_count"], 1)
        self.assertTrue(s["messages"][0]["addressed"])
        self.assertGreater(s["cursor"], cursor)
        code, text = req("/sync?%s&agent=Codex&since=%d" % (q(), s["cursor"]))
        self.assertIn("NEW: nothing since", text)

    def test_claim_409_and_tasks_endpoint(self):
        req("/claim", {"game": GAME, "agent": "Antigravity", "paths": ["assets/"]})
        code, r = req("/claim", {"game": GAME, "agent": "Claude", "paths": ["assets/x.png"]})
        self.assertEqual(code, 409)
        self.assertEqual(r["conflicts"][0]["agent"], "Antigravity")
        code, r = req("/tasks", {"game": GAME, "agent": "Claude", "title": "Write tests", "assignee": "Claude"})
        self.assertEqual(code, 200)
        code, s = req("/sync?%s&agent=Claude&format=json" % q())
        self.assertIn(r["task"]["id"], [t["id"] for t in s["your_tasks"]])

    def test_security_guards(self):
        code, _ = req("/post", {"game": GAME, "agent": "Human", "text": "spoof"})
        self.assertEqual(code, 403)  # agents cannot post as Human
        code, _ = req("/post", {"game": GAME, "agent": "Claude", "text": "x"}, {"Origin": "https://evil.example"})
        self.assertEqual(code, 403)  # CSRF
        code, _ = req("/health", headers={"Host": "evil.example"})
        self.assertEqual(code, 403)  # DNS rebinding
        code, _ = req("/stats?game=missing.txt")
        self.assertEqual(code, 404)
        code, r = req("/post", {"game": GAME, "agent": "Human", "text": "from the dashboard"}, {"X-AIC-Client": "web"})
        self.assertEqual(code, 200)

    def test_zz_access_keys(self):
        code, r = req("/api/config", {"auth": {"enabled": True}})
        admin = r["issued"]["admin_key"]
        try:
            self.assertEqual(req("/stats?" + q())[0], 401)
            self.assertEqual(req("/health")[0], 200)
            code, r = req("/api/keys", {"target": "Codex"}, {"X-AIC-Key": admin})
            codex = r["key"]
            code, r = req("/post", {"game": GAME, "agent": "Claude", "text": "impersonation"}, {"X-AIC-Key": codex})
            self.assertEqual(code, 403)
            code, r = req("/post", {"game": GAME, "text": "as myself"}, {"X-AIC-Key": codex})
            self.assertEqual((code, r["agent"]), (200, "Codex"))
            self.assertEqual(req("/api/config", {"auth": {"enabled": False}}, {"X-AIC-Key": codex})[0], 403)
            code, cfg = req("/api/config", headers={"X-AIC-Key": admin})
            self.assertNotIn(codex, json.dumps(cfg))  # secrets never leave the server
        finally:
            req("/api/config", {"auth": {"enabled": False}}, {"X-AIC-Key": admin})


class T5Hosted(unittest.TestCase):
    def test_mention_triggers_hosted_reply(self):
        purl = "http://127.0.0.1:%d/v1" % PROVIDER.server_address[1]
        code, r = req("/api/config", {
            "providers": {"custom": {"api_key": "sk-test-123456789", "base_url": purl}},
            "agents": [a for a in store.public_config()["agents"]] + [
                {"name": "Reviewer", "color": "#123456", "provider": "custom", "model": "fake-1", "role": "Code reviewer"}]})
        self.assertEqual(code, 200, r)
        self.assertNotIn("sk-test-123456789", json.dumps(r))
        code, r = req("/post", {"game": GAME, "agent": "Codex", "text": "@Reviewer is the save fix right?"})
        self.assertEqual(r["triggered"], ["Reviewer"])
        for _ in range(50):
            items, _ = cv.load_items(GAME)
            if items[-1]["agent"] == "Reviewer":
                break
            time.sleep(0.1)
        self.assertEqual(items[-1]["agent"], "Reviewer")
        self.assertTrue(items[-1]["text"].startswith("looks right"))  # own name prefix stripped
        call = FakeProvider.calls[-1]
        self.assertEqual(call["auth"], "Bearer sk-test-123456789")
        self.assertIn("Code reviewer", call["body"]["messages"][0]["content"])
        # the hosted reply mentioned @Codex; hosted replies must not trigger anything (no loops)
        n = len(FakeProvider.calls)
        time.sleep(0.4)
        self.assertEqual(len(FakeProvider.calls), n)


class T6Mcp(unittest.TestCase):
    def test_mcp_roundtrip(self):
        env = dict(os.environ, AIC_URL=BASE, AIC_AGENT="Cline", AIC_GAME=GAME)
        p = subprocess.Popen([sys.executable, os.path.join(ROOT, "aic_mcp.py")], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True, env=env)
        msgs = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "aic_sync", "arguments": {}}},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "aic_post", "arguments": {"text": "via mcp", "type": "Status"}}},
        ]
        out, _ = p.communicate("\n".join(json.dumps(m) for m in msgs) + "\n", timeout=30)
        res = {r["id"]: r for r in map(json.loads, out.strip().splitlines())}
        self.assertEqual(res[1]["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("aic_claim", [t["name"] for t in res[2]["result"]["tools"]])
        self.assertIn("SYNC cursor=", res[3]["result"]["content"][0]["text"])
        self.assertFalse(res[4]["result"]["isError"], res[4])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class FakeAnthropic(http.server.BaseHTTPRequestHandler):
    seen = []
    refuse = False

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeAnthropic.seen.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if FakeAnthropic.refuse:
            out = {"content": [], "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "cyber"}}
        else:
            out = {"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": "OK"}], "stop_reason": "end_turn"}
        raw = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class T7AnthropicWire(unittest.TestCase):
    def test_messages_api_shape_and_refusal(self):
        import aic_llm as llm
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeAnthropic)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            prov = {"kind": "anthropic", "base_url": "http://127.0.0.1:%d" % srv.server_address[1],
                    "api_key": "sk-ant-x", "label": "Anthropic"}
            self.assertEqual(llm.complete(prov, "claude-opus-5-5", "sys", "hi"), "OK")
            call = FakeAnthropic.seen[-1]
            self.assertEqual(call["path"], "/v1/messages")
            self.assertEqual(call["headers"]["x-api-key"], "sk-ant-x")
            self.assertEqual(call["headers"]["anthropic-version"], llm.ANTHROPIC_VERSION)
            self.assertNotIn("temperature", call["body"])
            self.assertNotIn("fallbacks", call["body"])  # only sent to api.anthropic.com
            FakeAnthropic.refuse = True
            with self.assertRaises(llm.LLMError):
                llm.complete(prov, "claude-opus-5-5", "sys", "hi")
        finally:
            srv.shutdown()
