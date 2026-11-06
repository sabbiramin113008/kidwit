import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import agent


class FakeUI:
    """Records events and answers approval prompts from a script."""

    def __init__(self, answers=()):
        self.events, self.answers = [], list(answers)

    def emit(self, kind, **data):
        self.events.append((kind, data))

    def ask(self, prompt):
        return self.answers.pop(0) if self.answers else None

    def kinds(self):
        return [k for k, _ in self.events]


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.ui = FakeUI()
        self.tools = self.make()

    def make(self, mode="auto", **kw):
        return agent.Tools(self.root, self.ui, mode, **kw)

    def put(self, name, content="", binary=False):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        (p.write_bytes if binary else p.write_text)(content)
        return p

    def call(self, name, **args):
        return self.tools.call(name, args)


class DispatchTests(ToolTestCase):
    def test_never_raises(self):
        self.assertEqual(self.tools.call("nope", {}), ("unknown tool: nope", True))
        self.assertTrue(self.call("read_file")[1])  # missing arg -> TypeError
        self.assertTrue(self.call("read_file", path="missing.txt")[1])  # OSError
        self.assertTrue(self.call("read_file", path="a", offset="x")[1])  # ValueError
        self.assertTrue(self.tools.call("read_file", "not a dict")[1])
        out, err = self.tools.call("run", {"_invalid_json": "{oops"})
        self.assertTrue(err)
        self.assertIn("not valid JSON", out)

    def test_output_is_clipped(self):
        self.put("big.txt", "x" * 50000)
        out, err = self.call("read_file", path="big.txt")
        self.assertFalse(err)
        self.assertLess(len(out), agent.MAX_TOOL_OUTPUT + 100)
        self.assertIn("clipped", out)


class PathJailTests(ToolTestCase):
    def test_relative_escape(self):
        self.assertTrue(self.call("read_file", path="../x")[1])
        self.assertTrue(self.call("write_file", path="a/../../x", content="1")[1])

    def test_absolute_outside(self):
        out, err = self.call("read_file", path="/etc/passwd")
        self.assertTrue(err)
        self.assertIn("outside", out)

    def test_symlink_escape(self):
        with tempfile.TemporaryDirectory() as outside:
            (Path(outside) / "secret.txt").write_text("secret")
            os.symlink(outside, self.root / "link")
            os.symlink(Path(outside) / "secret.txt", self.root / "file_link")
            self.assertTrue(self.call("read_file", path="link/secret.txt")[1])
            self.assertTrue(self.call("read_file", path="file_link")[1])
            self.assertTrue(self.call("write_file", path="link/new.txt", content="x")[1])
            self.assertFalse((Path(outside) / "new.txt").exists())
            self.assertNotIn("file_link", self.call("list_files")[0])
            self.assertEqual(self.call("search", regex="secret")[0], "no matches")

    def test_inside_symlink_ok(self):
        self.put("real.txt", "hello")
        os.symlink(self.root / "real.txt", self.root / "alias.txt")
        self.assertIn("hello", self.call("read_file", path="alias.txt")[0])


class ListSearchReadTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.put("top.py", "import os\n")
        self.put("src/m.py", "def f(): pass\n")
        self.put("src/a/n.py", "def g(): pass\n")
        self.put("node_modules/z.py", "def h(): pass\n")
        self.put(".git/config", "def i\n")
        self.put("bin.dat", b"def\0\1", binary=True)
        self.put("notes.txt", "one\ntwo\nthree\nfour\nfive\n")

    def files(self, **kw):
        return self.call("list_files", **kw)[0].split("\n")

    def test_list_skips_dirs(self):
        files = self.files()
        self.assertIn("src/m.py", files)
        self.assertFalse([f for f in files if f.startswith(("node_modules", ".git"))])

    def test_glob_patterns(self):
        self.assertEqual(sorted(self.files(pattern="*.py")), ["src/a/n.py", "src/m.py", "top.py"])
        self.assertEqual(sorted(self.files(pattern="src/**/*.py")), ["src/a/n.py", "src/m.py"])
        self.assertEqual(self.files(pattern="src/*.py"), ["src/m.py"])
        self.assertEqual(self.call("list_files", pattern="*.rs")[0], "(no files)")

    def test_list_cap(self):
        for i in range(1005):
            self.put(f"m/{i}")
        out = self.call("list_files", pattern="m/*")[0]
        self.assertEqual(len(out.split("\n")), 1001)
        self.assertIn("truncated", out.split("\n")[-1])

    def test_search(self):
        out = self.call("search", regex=r"def \w")[0].split("\n")
        self.assertEqual(sorted(out), ["src/a/n.py:1: def g(): pass", "src/m.py:1: def f(): pass"])
        self.assertEqual(self.call("search", regex="def", pattern="top.py")[0], "no matches")
        out, err = self.call("search", regex="(")
        self.assertTrue(err)
        self.assertIn("invalid regex", out)

    def test_search_skips_big_files_and_caps(self):
        self.put("huge.txt", "needle\n" + "x" * 2_100_000)
        self.assertEqual(self.call("search", regex="needle")[0], "no matches")
        self.put("lots.txt", "hit\n" * 300)
        out = self.call("search", regex="hit")[0]
        self.assertEqual(len(out.split("\n")), 201)
        self.assertIn("stopped at 200", out)

    def test_read_numbered_and_paged(self):
        self.assertEqual(self.call("read_file", path="notes.txt")[0].split("\n")[1], "     2\ttwo")
        out = self.call("read_file", path="notes.txt", offset=2, limit=2)[0]
        self.assertIn("     3\tthree", out)
        self.assertNotIn("four", out)
        self.assertIn("2 more lines; use offset=4", out)

    def test_read_binary_and_directory(self):
        self.assertIn("binary", self.call("read_file", path="bin.dat")[0])
        self.assertTrue(self.call("read_file", path="src")[1])


class EditTests(ToolTestCase):
    def test_write_new_and_overwrite(self):
        self.assertFalse(self.call("write_file", path="a/b.txt", content="one\n")[1])
        self.assertEqual((self.root / "a/b.txt").read_text(), "one\n")
        self.call("write_file", path="a/b.txt", content="two\n")
        self.assertEqual((self.root / "a/b.txt").read_text(), "two\n")
        self.assertTrue(self.call("write_file", path="a", content="x")[1])

    def test_edit_unique_match_rule(self):
        self.put("f.txt", "a b a\n")
        out, err = self.call("edit_file", path="f.txt", old_text="a", new_text="z")
        self.assertTrue(err)
        self.assertIn("2 places", out)
        self.assertTrue(self.call("edit_file", path="f.txt", old_text="q", new_text="z")[1])
        self.assertTrue(self.call("edit_file", path="f.txt", old_text="", new_text="z")[1])
        self.assertFalse(self.call("edit_file", path="f.txt", old_text="b", new_text="B")[1])
        self.assertFalse(self.call("edit_file", path="f.txt", old_text="a", new_text="z", replace_all=True)[1])
        self.assertEqual((self.root / "f.txt").read_text(), "z B z\n")

    def test_edit_crlf(self):
        self.put("w.txt", "x\r\ny\r\nz\r\n", binary=False)
        (self.root / "w.txt").write_bytes(b"x\r\ny\r\nz\r\n")
        self.assertFalse(self.call("edit_file", path="w.txt", old_text="x\ny", new_text="p\nq")[1])
        self.assertEqual((self.root / "w.txt").read_bytes(), b"p\r\nq\r\nz\r\n")
        self.call("write_file", path="keep.txt", content="a\r\nb\r\n")
        self.assertEqual((self.root / "keep.txt").read_bytes(), b"a\r\nb\r\n")

    def test_edit_shows_diff(self):
        self.put("f.txt", "one\ntwo\n")
        self.tools = self.make("ask")
        self.ui.answers = ["y"]
        self.call("edit_file", path="f.txt", old_text="two", new_text="2")
        kind, data = self.ui.events[0]
        self.assertEqual((kind, data["action"], data["target"]), ("approval", "edit", "f.txt"))
        self.assertIn("-two", data["detail"])
        self.assertIn("+2", data["detail"])


class RunTests(ToolTestCase):
    def test_exit_code_and_output(self):
        out, err = self.call("run", command="echo hi; echo oops 1>&2; exit 3")
        self.assertFalse(err)
        self.assertTrue(out.startswith("[exit code 3]"))
        self.assertIn("hi", out)
        self.assertIn("oops", out)
        self.assertIn(("output", {"line": "hi"}), self.ui.events)

    def test_runs_in_root_without_stdin(self):
        self.put("marker.txt")
        self.assertIn("marker.txt", self.call("run", command="ls")[0])
        self.assertTrue(self.call("run", command="cat")[0].startswith("[exit code 0]"))

    def test_timeout_kills_child_processes(self):
        start = time.time()
        out, _ = self.call("run", command="(sleep 60) & echo $!; sleep 60", timeout=1)
        self.assertLess(time.time() - start, 10)
        self.assertIn("timed out after 1s", out)
        pid = int(out.split("\n")[1])
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail("child process survived the timeout")


class ApprovalTests(ToolTestCase):
    def ask(self, *answers):
        self.tools = self.make("ask")
        self.ui.answers = list(answers)

    def test_yes(self):
        self.ask("y")
        self.assertFalse(self.call("write_file", path="a.txt", content="1")[1])
        self.assertTrue((self.root / "a.txt").exists())

    def test_always_switches_to_auto(self):
        self.ask("a")
        self.call("write_file", path="a.txt", content="1")
        self.call("write_file", path="b.txt", content="2")
        self.assertEqual(self.ui.kinds().count("approval"), 1)
        self.assertEqual(self.tools.mode, "auto")

    def test_no(self):
        self.ask("n")
        out, err = self.call("write_file", path="a.txt", content="1")
        self.assertTrue(err)
        self.assertEqual(out, "denied by user")
        self.assertFalse((self.root / "a.txt").exists())

    def test_feedback_reaches_model(self):
        self.ask("use a different name")
        out, err = self.call("run", command="touch x")
        self.assertTrue(err)
        self.assertIn("use a different name", out)
        self.assertFalse((self.root / "x").exists())

    def test_eof_means_no(self):
        self.ask()
        self.assertTrue(self.call("run", command="touch x")[1])
        self.assertFalse((self.root / "x").exists())

    def test_reads_never_ask(self):
        self.ask()
        self.put("f.txt", "x")
        self.call("read_file", path="f.txt")
        self.call("list_files")
        self.assertNotIn("approval", self.ui.kinds())

    def test_read_only_hides_and_refuses(self):
        self.tools = self.make("read-only")
        names = {t["name"] for t in self.tools.available()}
        self.assertEqual(names, {"list_files", "search", "read_file"})
        out, err = self.call("write_file", path="a.txt", content="1")
        self.assertTrue(err)
        self.assertIn("read-only", out)
        self.assertFalse((self.root / "a.txt").exists())
        self.assertTrue(self.call("run", command="touch x")[1])


class AuditTests(ToolTestCase):
    def test_audit_log_and_gitignore(self):
        self.call("write_file", path="a.txt", content="1")
        self.call("edit_file", path="a.txt", old_text="1", new_text="2")
        self.call("run", command="true")
        lines = (self.root / ".agent/audit.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(x)["action"] for x in lines], ["write", "edit", "run"])
        self.assertEqual((self.root / ".agent/.gitignore").read_text(), "*\n")

    def test_no_log(self):
        self.tools = self.make(log=False)
        self.call("write_file", path="a.txt", content="1")
        self.assertFalse((self.root / ".agent").exists())

    def test_logging_failure_is_swallowed(self):
        self.put(".agent", "i am a file, not a directory")
        self.assertFalse(self.call("write_file", path="a.txt", content="1")[1])


class FakeAPI:
    """Local HTTP server replaying scripted (status, body) replies and recording requests."""

    def __init__(self, replies):
        self.replies, self.requests = list(replies), []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                                       "body": json.loads(self.rfile.read(length))})
                status, body = outer.replies.pop(0) if outer.replies else (500, {"error": "script ran out"})
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ProviderTestCase(unittest.TestCase):
    def setUp(self):
        agent.BACKOFF = 0
        self.addCleanup(setattr, agent, "BACKOFF", 2)

    def api(self, *replies):
        fake = FakeAPI(replies)
        self.addCleanup(fake.close)
        return fake


HISTORY = [
    {"role": "user", "text": "fix it"},
    {"role": "assistant", "text": "looking", "calls": [{"id": "c1", "name": "read_file", "input": {"path": "a.py"}}]},
    {"role": "tool", "results": [{"id": "c1", "output": "code", "is_error": False}]},
]


class PostJsonTests(ProviderTestCase):
    def test_retries_529_then_succeeds(self):
        fake = self.api((529, {"error": "overloaded"}), (503, {}), (200, {"ok": True}))
        self.assertEqual(agent.post_json(fake.url, {}, {"a": 1}), {"ok": True})
        self.assertEqual(len(fake.requests), 3)

    def test_gives_up_after_four_attempts(self):
        fake = self.api(*[(529, {"error": "overloaded"})] * 5)
        with self.assertRaises(agent.AgentError) as cm:
            agent.post_json(fake.url, {}, {})
        self.assertIn("4 attempts", str(cm.exception))
        self.assertEqual(len(fake.requests), 4)

    def test_400_fails_immediately_with_body(self):
        fake = self.api((400, {"error": "bad model name"}), (200, {}))
        with self.assertRaises(agent.AgentError) as cm:
            agent.post_json(fake.url, {}, {})
        self.assertIn("400", str(cm.exception))
        self.assertIn("bad model name", str(cm.exception))
        self.assertEqual(len(fake.requests), 1)

    def test_network_error_is_retried_then_reported(self):
        with self.assertRaises(agent.AgentError):
            agent.post_json("http://127.0.0.1:1/x", {}, {})


class AnthropicTests(ProviderTestCase):
    REPLY = {"content": [{"type": "text", "text": "ok"},
                         {"type": "tool_use", "id": "t9", "name": "run", "input": {"command": "ls"}}],
             "usage": {"input_tokens": 10, "cache_read_input_tokens": 5,
                       "cache_creation_input_tokens": 3, "output_tokens": 7}}

    def test_request_shape_and_parsing(self):
        fake = self.api((200, self.REPLY))
        p = agent.AnthropicProvider("sk-test", "model-x", fake.url)
        text, calls, tokens = p.chat("SYS", HISTORY, agent.TOOLS)
        req = fake.requests[0]
        self.assertEqual(req["path"], "/v1/messages")
        self.assertEqual(req["headers"]["x-api-key"], "sk-test")
        self.assertEqual(req["headers"]["anthropic-version"], "2023-06-01")
        body = req["body"]
        self.assertEqual((body["model"], body["max_tokens"]), ("model-x", 8192))
        self.assertEqual(body["system"][0]["text"], "SYS")
        self.assertEqual(body["system"][0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(body["tools"][0].keys(), {"name", "description", "input_schema"})
        self.assertEqual(body["messages"][-1]["content"][-1]["cache_control"], {"type": "ephemeral"})
        result = body["messages"][-1]["content"][0]
        self.assertEqual((result["type"], result["tool_use_id"], result["is_error"]), ("tool_result", "c1", False))
        self.assertEqual(body["messages"][1]["content"][1]["type"], "tool_use")
        self.assertEqual((text, calls), ("ok", [{"id": "t9", "name": "run", "input": {"command": "ls"}}]))
        self.assertEqual(tokens, (18, 7))

    def test_consecutive_user_messages_merge(self):
        history = HISTORY + [{"role": "user", "text": "try again"}]
        msgs = agent.AnthropicProvider.convert(history)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual([b["type"] for b in msgs[-1]["content"]], ["tool_result", "text"])
        two_users = agent.AnthropicProvider.convert([{"role": "user", "text": "a"}, {"role": "user", "text": "b"}])
        self.assertEqual(len(two_users), 1)
        self.assertEqual(len(two_users[0]["content"]), 2)

    def test_http_error_surfaces(self):
        fake = self.api((400, {"error": {"message": "nope"}}))
        with self.assertRaises(agent.AgentError):
            agent.AnthropicProvider("k", "m", fake.url).chat("s", HISTORY, [])


class OpenAITests(ProviderTestCase):
    def reply(self, message, usage=None):
        return (200, {"choices": [{"message": message}], "usage": usage or {}})

    def test_request_shape_and_parsing(self):
        call = {"id": "x1", "type": "function", "function": {"name": "run", "arguments": '{"command": "ls"}'}}
        fake = self.api(self.reply({"content": None, "tool_calls": [call]},
                                   {"prompt_tokens": 4, "completion_tokens": 2}))
        p = agent.OpenAIProvider("sk-o", "gpt-x", fake.url)
        text, calls, tokens = p.chat("SYS", HISTORY, agent.TOOLS)
        req = fake.requests[0]
        self.assertEqual(req["path"], "/chat/completions")
        self.assertEqual(req["headers"]["authorization"], "Bearer sk-o")
        msgs = req["body"]["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "tool"])
        self.assertEqual(msgs[2]["tool_calls"][0]["function"]["arguments"], '{"path": "a.py"}')
        self.assertEqual(msgs[3], {"role": "tool", "tool_call_id": "c1", "content": "code"})
        tool = req["body"]["tools"][0]
        self.assertEqual(tool["type"], "function")
        self.assertIn("parameters", tool["function"])
        self.assertEqual((text, calls, tokens), ("", [{"id": "x1", "name": "run", "input": {"command": "ls"}}], (4, 2)))

    def test_no_auth_header_without_key(self):
        fake = self.api(self.reply({"content": "hi"}))
        agent.OpenAIProvider(None, "local", fake.url).chat("s", HISTORY[:1], [])
        self.assertNotIn("authorization", fake.requests[0]["headers"])
        self.assertNotIn("tools", fake.requests[0]["body"])

    def test_invalid_json_arguments(self):
        call = {"id": "x1", "type": "function", "function": {"name": "run", "arguments": "{broken"}}
        fake = self.api(self.reply({"content": "", "tool_calls": [call]}))
        _, calls, _ = agent.OpenAIProvider("k", "m", fake.url).chat("s", HISTORY[:1], [])
        self.assertEqual(calls[0]["input"], {"_invalid_json": "{broken"})
        out, err = agent.Tools(".", None).call("run", calls[0]["input"])
        self.assertTrue(err)
        self.assertIn("not valid JSON", out)

    def test_content_null_rules(self):
        msgs = agent.OpenAIProvider.convert("s", [
            {"role": "assistant", "text": "", "calls": [{"id": "1", "name": "run", "input": {}}]},
            {"role": "assistant", "text": "", "calls": []}])
        self.assertIsNone(msgs[1]["content"])
        self.assertEqual(msgs[2]["content"], "")

    def test_unexpected_reply(self):
        fake = self.api((200, {"oops": 1}))
        with self.assertRaises(agent.AgentError):
            agent.OpenAIProvider("k", "m", fake.url).chat("s", HISTORY[:1], [])


if __name__ == "__main__":
    unittest.main()
