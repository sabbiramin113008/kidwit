import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import kidwit  # noqa: E402
from evals import run_evals
from evals.tasks import TASKS


class FakeUI:
    """Records events and answers approval prompts from a script."""

    def __init__(self, answers=()):
        self.events, self.answers = [], list(answers)

    def emit(self, kind, **data):
        self.events.append((kind, data))

    def ask(self, prompt, code="0"):
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
        return kidwit.Tools(self.root, self.ui, mode, **kw)

    def put(self, name, content="", binary=False):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content if binary else content.encode())
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
        self.assertLess(len(out), kidwit.MAX_TOOL_OUTPUT + 100)
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
        code = "import sys; print('hi'); print('oops', file=sys.stderr); sys.exit(3)"
        out, err = self.call("run", command=f'"{sys.executable}" -c "{code}"')
        self.assertFalse(err)
        self.assertTrue(out.startswith("[exit code 3]"))
        self.assertIn("hi", out)
        self.assertIn("oops", out)
        self.assertIn(("output", {"line": "hi"}), self.ui.events)

    def test_python_does_not_write_stale_bytecode(self):
        self.put("m.py", "print('v1')\n")
        self.call("run", command="python3 m.py")
        self.put("m.py", "print('v2')\n")  # same size, same second: a stale .pyc would print v1
        self.assertIn("v2", self.call("run", command="python3 m.py")[0])
        self.assertFalse((self.root / "__pycache__").exists())

    def test_runs_in_root_without_stdin(self):
        self.put("marker.txt")
        self.assertIn("marker.txt", self.call("run", command="ls")[0])
        self.assertTrue(self.call("run", command="cat")[0].startswith("[exit code 0]"))

    def test_timeout_returns_promptly(self):
        start = time.time()
        out, _ = self.call("run", command=f'"{sys.executable}" -c "import time; time.sleep(60)"', timeout=1)
        self.assertLess(time.time() - start, 10)
        self.assertIn("timed out after 1s", out)

    @unittest.skipIf(os.name == "nt", "uses POSIX shell syntax and signals")
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

    def test_always_covers_only_that_kind_of_action(self):
        self.ask("a", "n")
        self.call("write_file", path="a.txt", content="1")
        self.call("write_file", path="b.txt", content="2")
        self.call("edit_file", path="a.txt", old_text="1", new_text="3")
        self.assertEqual(self.ui.kinds().count("approval"), 1)
        self.assertEqual(self.tools.mode, "ask")
        self.assertTrue(self.call("run", command="touch x")[1])  # commands still ask
        self.assertEqual(self.ui.kinds().count("approval"), 2)
        self.assertFalse((self.root / "x").exists())

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
        kidwit.BACKOFF = 0
        self.addCleanup(setattr, kidwit, "BACKOFF", 2)

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
        self.assertEqual(kidwit.post_json(fake.url, {}, {"a": 1}), {"ok": True})
        self.assertEqual(len(fake.requests), 3)

    def test_gives_up_after_four_attempts(self):
        fake = self.api(*[(529, {"error": "overloaded"})] * 5)
        with self.assertRaises(kidwit.AgentError) as cm:
            kidwit.post_json(fake.url, {}, {})
        self.assertIn("4 attempts", str(cm.exception))
        self.assertEqual(len(fake.requests), 4)

    def test_400_fails_immediately_with_body(self):
        fake = self.api((400, {"error": "bad model name"}), (200, {}))
        with self.assertRaises(kidwit.AgentError) as cm:
            kidwit.post_json(fake.url, {}, {})
        self.assertIn("400", str(cm.exception))
        self.assertIn("bad model name", str(cm.exception))
        self.assertEqual(len(fake.requests), 1)

    def test_network_error_is_retried_then_reported(self):
        with self.assertRaises(kidwit.AgentError):
            kidwit.post_json("http://127.0.0.1:1/x", {}, {})


class AnthropicTests(ProviderTestCase):
    REPLY = {"content": [{"type": "text", "text": "ok"},
                         {"type": "tool_use", "id": "t9", "name": "run", "input": {"command": "ls"}}],
             "usage": {"input_tokens": 10, "cache_read_input_tokens": 5,
                       "cache_creation_input_tokens": 3, "output_tokens": 7}}

    def test_request_shape_and_parsing(self):
        fake = self.api((200, self.REPLY))
        p = kidwit.AnthropicProvider("sk-test", "model-x", fake.url)
        text, calls, tokens = p.chat("SYS", HISTORY, kidwit.TOOLS)
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
        msgs = kidwit.AnthropicProvider.convert(history)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual([b["type"] for b in msgs[-1]["content"]], ["tool_result", "text"])
        two_users = kidwit.AnthropicProvider.convert([{"role": "user", "text": "a"}, {"role": "user", "text": "b"}])
        self.assertEqual(len(two_users), 1)
        self.assertEqual(len(two_users[0]["content"]), 2)

    def test_http_error_surfaces(self):
        fake = self.api((400, {"error": {"message": "nope"}}))
        with self.assertRaises(kidwit.AgentError):
            kidwit.AnthropicProvider("k", "m", fake.url).chat("s", HISTORY, [])


class OpenAITests(ProviderTestCase):
    def reply(self, message, usage=None):
        return (200, {"choices": [{"message": message}], "usage": usage or {}})

    def test_request_shape_and_parsing(self):
        call = {"id": "x1", "type": "function", "function": {"name": "run", "arguments": '{"command": "ls"}'}}
        fake = self.api(self.reply({"content": None, "tool_calls": [call]},
                                   {"prompt_tokens": 4, "completion_tokens": 2}))
        p = kidwit.OpenAIProvider("sk-o", "gpt-x", fake.url)
        text, calls, tokens = p.chat("SYS", HISTORY, kidwit.TOOLS)
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
        kidwit.OpenAIProvider(None, "local", fake.url).chat("s", HISTORY[:1], [])
        self.assertNotIn("authorization", fake.requests[0]["headers"])

    def test_invalid_json_arguments(self):
        call = {"id": "x1", "type": "function", "function": {"name": "run", "arguments": "{broken"}}
        fake = self.api(self.reply({"content": "", "tool_calls": [call]}))
        _, calls, _ = kidwit.OpenAIProvider("k", "m", fake.url).chat("s", HISTORY[:1], [])
        self.assertEqual(calls[0]["input"], {"_invalid_json": "{broken"})
        out, err = kidwit.Tools(".", None).call("run", calls[0]["input"])
        self.assertTrue(err)
        self.assertIn("not valid JSON", out)

    def test_content_null_rules(self):
        msgs = kidwit.OpenAIProvider.convert("s", [
            {"role": "assistant", "text": "", "calls": [{"id": "1", "name": "run", "input": {}}]},
            {"role": "assistant", "text": "", "calls": []}])
        self.assertIsNone(msgs[1]["content"])
        self.assertEqual(msgs[2]["content"], "")

    def test_unexpected_reply(self):
        fake = self.api((200, {"oops": 1}))
        with self.assertRaises(kidwit.AgentError):
            kidwit.OpenAIProvider("k", "m", fake.url).chat("s", HISTORY[:1], [])


class ScriptedProvider:
    """Replays scripted (text, calls, tokens) replies; a BaseException step is raised instead."""

    def __init__(self, *steps):
        self.steps, self.seen = list(steps), []

    def chat(self, system, history, tools):
        self.seen.append((system, json.loads(json.dumps(history)), [t["name"] for t in tools]))
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def call(id, name, **input):
    return {"id": id, "name": name, "input": input}


def assert_valid_history(test, history):
    """Every assistant tool call must be answered by the very next tool message."""
    for i, m in enumerate(history):
        if m["role"] == "assistant" and m.get("calls"):
            test.assertLess(i + 1, len(history), "tool calls without results")
            test.assertEqual(history[i + 1]["role"], "tool")
            test.assertEqual([r["id"] for r in history[i + 1]["results"]], [c["id"] for c in m["calls"]])


class AgentTestCase(ToolTestCase):
    def agent(self, *steps, mode="auto"):
        self.provider = ScriptedProvider(*steps)
        self.tools = self.make(mode)
        return kidwit.Agent(self.provider, self.tools, self.ui, "SYSTEM")

    def events(self, kind):
        return [d for k, d in self.ui.events if k == kind]


class AgentLoopTests(AgentTestCase):
    def test_multi_step_task(self):
        self.put("calc.py", "def add(a, b):\n    return a - b\n")
        self.put("check.py", "from calc import add\nassert add(2, 3) == 5, 'add is broken'\n")
        bot = self.agent(
            ("Let me look.", [call("1", "read_file", path="calc.py")], (100, 10)),
            ("", [call("2", "edit_file", path="calc.py", old_text="a - b", new_text="a + b")], (150, 20)),
            ("", [call("3", "run", command="python3 check.py")], (200, 5)),
            ("Fixed: add now adds.", [], (250, 15)),
        )
        self.assertEqual(bot.run("fix add"), "Fixed: add now adds.")
        self.assertIn("a + b", (self.root / "calc.py").read_text())
        self.assertEqual(self.events("result")[2]["output"].split("\n")[0], "[exit code 0]")
        self.assertEqual(self.events("done"), [{"turns": 4, "input_tokens": 700, "output_tokens": 50}])
        self.assertEqual(self.ui.kinds()[:3], ["text", "tool", "result"])
        self.assertEqual([m["role"] for m in bot.history],
                         ["user", "assistant", "tool", "assistant", "tool", "assistant", "tool", "assistant"])
        assert_valid_history(self, bot.history)

    def test_tool_errors_go_back_to_the_model(self):
        bot = self.agent(("", [call("1", "read_file", path="missing.py"), call("2", "bogus")], (1, 1)),
                         ("I could not find it.", [], (1, 1)))
        self.assertEqual(bot.run("go"), "I could not find it.")
        self.assertEqual([r["is_error"] for r in bot.history[2]["results"]], [True, True])

    def test_denied_approval_is_reported_to_model(self):
        bot = self.agent(("", [call("1", "run", command="touch x")], (1, 1)), ("ok", [], (1, 1)), mode="ask")
        self.ui.answers = ["please do not"]
        bot.run("go")
        self.assertIn("please do not", bot.history[2]["results"][0]["output"])
        self.assertFalse((self.root / "x").exists())

    def test_read_only_offers_no_write_tools(self):
        bot = self.agent(("done", [], (1, 1)), mode="read-only")
        bot.run("look")
        self.assertEqual(self.provider.seen[0][2], ["list_files", "search", "read_file"])

    def test_turn_limit(self):
        bot = self.agent(*[("", [call(str(i), "list_files")], (1, 1)) for i in range(kidwit.MAX_TURNS)])
        self.assertIsNone(bot.run("loop forever"))
        self.assertEqual(len(self.provider.seen), kidwit.MAX_TURNS)
        self.assertIn("turns", self.events("error")[0]["message"])
        assert_valid_history(self, bot.history)

    def test_empty_response_keeps_history_valid(self):
        bot = self.agent(("", [], (1, 1)))
        bot.run("hi")
        self.assertTrue(bot.history[-1]["text"])

    def test_api_error_returns_none(self):
        bot = self.agent(kidwit.AgentError("HTTP 400: nope"))
        self.assertIsNone(bot.run("hi"))
        self.assertEqual(self.events("error"), [{"message": "HTTP 400: nope"}])


class InterruptTests(AgentTestCase):
    def test_interrupt_during_tool_leaves_valid_history(self):
        bot = self.agent(("", [call("1", "list_files"), call("2", "run", command="sleep 5")], (1, 1)))

        class Boom(kidwit.Tools):
            def call(self, name, args):
                if name == "run":
                    raise KeyboardInterrupt
                return super().call(name, args)

        bot.tools = Boom(self.root, self.ui, "auto")
        self.assertIsNone(bot.run("go"))
        self.assertEqual(self.events("error"), [{"message": "interrupted"}])
        assert_valid_history(self, bot.history)
        self.assertTrue(all(r["is_error"] for r in bot.history[-1]["results"]))

    def test_next_task_after_interrupt_is_valid_for_both_apis(self):
        bot = self.agent(("", [call("1", "list_files")], (1, 1)))
        bot.tools = type("T", (kidwit.Tools,), {"call": lambda *a: (_ for _ in ()).throw(KeyboardInterrupt)})(
            self.root, self.ui, "auto")
        bot.run("first")
        bot.history.append({"role": "user", "text": "second"})
        assert_valid_history(self, bot.history)
        msgs = kidwit.AnthropicProvider.convert(bot.history)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user"])
        self.assertEqual([b["type"] for b in msgs[-1]["content"]], ["tool_result", "text"])
        roles = [m["role"] for m in kidwit.OpenAIProvider.convert("s", bot.history)]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "user"])

    def test_repair_is_a_no_op_when_nothing_is_pending(self):
        bot = self.agent()
        bot.repair()
        bot.history = [{"role": "user", "text": "x"}]
        bot.repair()
        self.assertEqual(len(bot.history), 1)


class TrimTests(AgentTestCase):
    def history(self, n, size):
        h = [{"role": "user", "text": "task"}]
        for i in range(n):
            h.append({"role": "assistant", "text": "", "calls": [call(str(i), "read_file")]})
            h.append({"role": "tool", "results": [{"id": str(i), "output": "x" * size, "is_error": False}]})
        return h

    def test_small_history_is_untouched(self):
        bot = self.agent()
        bot.history = self.history(3, 1000)
        before = json.dumps(bot.history)
        bot.trim()
        self.assertEqual(json.dumps(bot.history), before)

    def test_trims_oldest_first_and_keeps_last_two_messages(self):
        bot = self.agent()
        bot.history = self.history(8, 100000)
        bot.trim()
        outputs = [r["output"] for m in bot.history if m["role"] == "tool" for r in m["results"]]
        self.assertEqual(outputs[0], kidwit.OLD_OUTPUT)
        self.assertEqual(outputs[-1], "x" * 100000)
        self.assertLessEqual(len(json.dumps(bot.history)), kidwit.CONTEXT_BUDGET + 1000)
        self.assertGreater(outputs.count(kidwit.OLD_OUTPUT), 0)
        self.assertLess(outputs.count(kidwit.OLD_OUTPUT), len(outputs))

    def test_never_touches_last_two_messages_even_over_budget(self):
        bot = self.agent()
        bot.history = self.history(2, 300000)
        bot.trim()
        self.assertEqual(bot.history[-1]["results"][0]["output"], "x" * 300000)

    def test_short_outputs_are_kept(self):
        bot = self.agent()
        bot.history = self.history(8, 150) + self.history(2, 300000)[1:]
        bot.trim()
        self.assertEqual(bot.history[2]["results"][0]["output"], "x" * 150)


class SystemPromptTests(ToolTestCase):
    def test_includes_root_os_and_shell(self):
        prompt = kidwit.build_system(self.root)
        self.assertIn(str(self.root), prompt)
        self.assertIn("kidwit", prompt)
        self.assertNotIn("Project instructions", prompt)

    def test_agents_md_then_claude_md(self):
        self.put("CLAUDE.md", "claude rules")
        self.assertIn("claude rules", kidwit.build_system(self.root))
        self.put("AGENTS.md", "agents rules")
        prompt = kidwit.build_system(self.root)
        self.assertIn("agents rules", prompt)
        self.assertNotIn("claude rules", prompt)

    def test_instructions_are_clipped(self):
        self.put("AGENTS.md", "y" * 50000)
        self.assertLess(kidwit.build_system(self.root).count("y"), 20100)


def args(**kw):
    return SimpleNamespace(**{"provider": None, "model": None, "base_url": None, **kw})


# Windows cannot start a subprocess without these, so tests that clear the environment keep them.
SYSTEM_ENV = {k: os.environ[k] for k in ("SYSTEMROOT", "COMSPEC", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "WINDIR")
              if k in os.environ}


@contextlib.contextmanager
def env(**values):
    with mock.patch.dict(os.environ, {**SYSTEM_ENV, **values}, clear=True):
        yield


class ProviderSelectionTests(unittest.TestCase):
    def test_anthropic_when_key_is_set(self):
        with env(ANTHROPIC_API_KEY="k", OPENAI_API_KEY="o", AGENT_MODEL="m"):
            p = kidwit.make_provider(args())
        self.assertIsInstance(p, kidwit.AnthropicProvider)
        self.assertEqual((p.key, p.model), ("k", "m"))

    def test_default_model_for_anthropic(self):
        with env(ANTHROPIC_API_KEY="k"):
            self.assertEqual(kidwit.make_provider(args()).model, kidwit.DEFAULT_MODEL)

    def test_flag_beats_env_beats_key(self):
        with env(ANTHROPIC_API_KEY="k", OPENAI_API_KEY="o", AGENT_PROVIDER="openai", AGENT_MODEL="m"):
            self.assertIsInstance(kidwit.make_provider(args()), kidwit.OpenAIProvider)
            self.assertIsInstance(kidwit.make_provider(args(provider="anthropic")), kidwit.AnthropicProvider)

    def test_openai_compatible_local_server_needs_no_key(self):
        with env(AGENT_BASE_URL="http://localhost:11434/v1", AGENT_MODEL="llama"):
            p = kidwit.make_provider(args())
        self.assertIsInstance(p, kidwit.OpenAIProvider)
        self.assertEqual((p.key, p.base), (None, "http://localhost:11434/v1"))

    def test_flags_override_env(self):
        with env(OPENAI_API_KEY="o", AGENT_MODEL="env-model", AGENT_BASE_URL="http://env"):
            p = kidwit.make_provider(args(model="flag-model", base_url="http://flag/"))
        self.assertEqual((p.model, p.base), ("flag-model", "http://flag"))

    def test_clear_errors(self):
        cases = [({}, args(), "no provider configured"),
                 ({"OPENAI_API_KEY": "o"}, args(), "AGENT_MODEL"),
                 ({}, args(provider="anthropic"), "ANTHROPIC_API_KEY"),
                 ({"AGENT_PROVIDER": "gemini"}, args(), "unknown provider")]
        for values, a, message in cases:
            with self.subTest(message), env(**values), self.assertRaises(kidwit.AgentError) as cm:
                kidwit.make_provider(a)
            self.assertIn(message, str(cm.exception))


class CliTests(ToolTestCase):
    def run_main(self, *argv, provider=None, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        patches = [contextlib.redirect_stdout(out), contextlib.redirect_stderr(err),
                   mock.patch("sys.stdin", io.StringIO(stdin)), env()]
        if provider:
            patches.append(mock.patch.object(kidwit, "make_provider", lambda a: provider))
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            code = kidwit.main(["--root", str(self.root), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_version(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            kidwit.main(["--version"])
        self.assertEqual(cm.exception.code, 0)
        self.assertIn(kidwit.VERSION, out.getvalue())

    def test_missing_config_exits_1_with_message(self):
        code, out, err = self.run_main("do it")
        self.assertEqual(code, 1)
        self.assertIn("no provider configured", err)

    def test_missing_config_in_json_mode_is_an_event(self):
        code, out, _ = self.run_main("--json", "do it")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out)["type"], "error")

    def test_bad_root(self):
        code, _, err = self.run_main("--root", "/definitely/not/here", "x")
        self.assertEqual(code, 1)
        self.assertIn("not a directory", err)

    def test_one_shot_exit_codes(self):
        ok = ScriptedProvider(("all done", [], (1, 1)))
        code, out, _ = self.run_main("fix", "the", "bug", provider=ok)
        self.assertEqual(code, 0)
        self.assertIn("all done", out)
        self.assertEqual(ok.seen[0][1][0]["text"], "fix the bug")
        bad = ScriptedProvider(kidwit.AgentError("boom"))
        code, _, err = self.run_main("fix", provider=bad)
        self.assertEqual(code, 1)
        self.assertIn("boom", err)

    def test_yes_and_read_only_modes(self):
        p = ScriptedProvider(("ok", [], (1, 1)))
        self.run_main("--read-only", "x", provider=p)
        self.assertEqual(p.seen[0][2], ["list_files", "search", "read_file"])
        p = ScriptedProvider(("", [call("1", "write_file", path="a.txt", content="hi")], (1, 1)), ("ok", [], (1, 1)))
        self.run_main("--yes", "x", provider=p)
        self.assertEqual((self.root / "a.txt").read_text(), "hi")
        self.assertTrue((self.root / ".agent/audit.jsonl").exists())

    def test_no_log_flag(self):
        p = ScriptedProvider(("", [call("1", "write_file", path="a.txt", content="hi")], (1, 1)), ("ok", [], (1, 1)))
        self.run_main("--yes", "--no-log", "x", provider=p)
        self.assertFalse((self.root / ".agent").exists())

    def test_yes_and_read_only_conflict(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            kidwit.main(["--yes", "--read-only", "x"])

    def test_repl_clear_and_exit(self):
        p = ScriptedProvider(("one", [], (1, 1)), ("two", [], (1, 1)))
        code, out, _ = self.run_main("--json", provider=p, stdin="first\n\n/clear\nsecond\n/exit\nnever\n")
        self.assertEqual(code, 0)
        self.assertEqual(p.seen[1][1], [{"role": "user", "text": "second"}])
        self.assertEqual(len(p.steps), 0)

    def test_repl_verbose_toggle(self):
        p = ScriptedProvider(("", [call("1", "run", command="echo hello-from-run")], (1, 1)), ("ok", [], (1, 1)))
        with mock.patch.object(kidwit.UI, "ask", side_effect=["/verbose", "go", "/verbose", None]):
            code, out, _ = self.run_main("--yes", provider=p)
        self.assertEqual(code, 0)
        self.assertIn("verbose on", out)
        self.assertIn("hello-from-run", out)
        self.assertIn("verbose off", out)

    def test_repl_prompt_is_double_arrow(self):
        with mock.patch.object(kidwit.UI, "ask", side_effect=[None]) as ask:
            self.run_main("--json", provider=ScriptedProvider())
        self.assertEqual(ask.call_args_list[0].args, (">> ", "1;32"))

    def test_repl_undo_reverts_and_tells_the_model(self):
        p = ScriptedProvider(("", [call("1", "write_file", path="a.txt", content="hi")], (1, 1)), ("ok", [], (1, 1)),
                             ("next", [], (1, 1)))
        with mock.patch.object(kidwit.UI, "ask", side_effect=["make a.txt", "/undo", "what now", None]):
            code, out, _ = self.run_main("--yes", provider=p)
        self.assertEqual(code, 0)
        self.assertFalse((self.root / "a.txt").exists())
        self.assertIn("reverted a.txt", out)
        last = p.seen[-1][1]
        self.assertIn("/undo: reverted a.txt", last[-2]["text"])
        self.assertEqual(last[-1], {"role": "user", "text": "what now"})

    def test_repl_quits_on_eof(self):
        code, _, _ = self.run_main("--json", provider=ScriptedProvider(), stdin="")
        self.assertEqual(code, 0)

    def test_json_mode_emits_only_json_lines(self):
        p = ScriptedProvider(("", [call("1", "list_files")], (3, 4)), ("done", [], (5, 6)))
        _, out, _ = self.run_main("--json", "go", provider=p)
        events = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([e["type"] for e in events], ["tool", "result", "text", "done"])
        self.assertEqual(events[-1], {"type": "done", "turns": 2, "input_tokens": 8, "output_tokens": 10})


class EndToEndTests(ProviderTestCase):
    """The real CLI as a subprocess, --json mode, against a fake OpenAI-compatible server."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def cli(self, *argv, stdin="", env_extra=None):
        env = {**SYSTEM_ENV, "PATH": os.environ["PATH"], "HOME": str(self.root), **(env_extra or {})}
        proc = subprocess.run([sys.executable, str(Path(kidwit.__file__)), "--root", str(self.root), *argv],
                              input=stdin, capture_output=True, text=True, env=env, timeout=60)
        return proc, [json.loads(line) for line in proc.stdout.splitlines()]

    def script(self):
        write = {"id": "call_1", "type": "function", "function": {
            "name": "write_file", "arguments": json.dumps({"path": "hello.txt", "content": "hi\n"})}}
        return self.api(
            (200, {"choices": [{"message": {"content": None, "tool_calls": [write]}}],
                   "usage": {"prompt_tokens": 10, "completion_tokens": 2}}),
            (200, {"choices": [{"message": {"content": "Created hello.txt."}}],
                   "usage": {"prompt_tokens": 20, "completion_tokens": 3}}))

    def run_task(self, stdin):
        fake = self.script()
        proc, events = self.cli("--json", "--provider", "openai", "--model", "fake-model",
                                "--base-url", fake.url, "create hello.txt", stdin=stdin)
        return fake, proc, events

    def test_approved_write(self):
        fake, proc, events = self.run_task("y\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([e["type"] for e in events], ["tool", "approval", "result", "text", "done"])
        approval = events[1]
        self.assertEqual((approval["action"], approval["target"]), ("write", "hello.txt"))
        self.assertEqual((self.root / "hello.txt").read_text(), "hi\n")
        self.assertEqual(events[-1], {"type": "done", "turns": 2, "input_tokens": 30, "output_tokens": 5})
        self.assertEqual(events[2]["is_error"], False)
        second = fake.requests[1]["body"]["messages"]
        self.assertEqual(second[-1]["role"], "tool")
        self.assertEqual(second[-1]["tool_call_id"], "call_1")
        self.assertNotIn("authorization", fake.requests[0]["headers"])
        self.assertEqual(fake.requests[0]["body"]["model"], "fake-model")

    def test_eof_at_approval_means_no(self):
        _, proc, events = self.run_task("")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = [e for e in events if e["type"] == "result"][0]
        self.assertTrue(result["is_error"])
        self.assertIn("denied", result["output"])
        self.assertFalse((self.root / "hello.txt").exists())

    def test_feedback_answer_is_passed_to_the_model(self):
        fake, _, _ = self.run_task("write it somewhere else\n")
        sent = fake.requests[1]["body"]["messages"][-1]["content"]
        self.assertIn("write it somewhere else", sent)

    def test_missing_key_error(self):
        proc, events = self.cli("--json", "hello")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertIn("no provider configured", events[0]["message"])
        proc, _ = self.cli("hello", env_extra={"AGENT_PROVIDER": "anthropic"})
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ANTHROPIC_API_KEY", proc.stderr)

    def test_api_failure_exits_1(self):
        fake = self.api((400, {"error": "bad request"}))
        proc, events = self.cli("--json", "--provider", "openai", "--model", "m",
                                "--base-url", fake.url, "go")
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("bad request", events[-1]["message"])


class BannerTests(unittest.TestCase):
    def banner(self, color):
        ui = kidwit.UI()
        ui.color = color
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ui.banner()
        return out.getvalue()

    def test_banner_on_color_terminal(self):
        out = self.banner(True)
        self.assertIn("█", out)
        self.assertIn("\033[", out)

    def test_no_banner_without_color(self):
        self.assertEqual(self.banner(False), "")

    def test_no_banner_in_json_or_piped_runs(self):
        self.assertFalse(kidwit.UI(json_mode=True).color)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch("sys.stdin", io.StringIO("")):
            kidwit.main(["--json", "--provider", "openai", "--model", "m", "--base-url", "http://127.0.0.1:1"])
        self.assertNotIn("█", out.getvalue())


class QuietOutputTests(unittest.TestCase):
    def show(self, verbose, *events):
        ui, out = kidwit.UI(verbose=verbose), io.StringIO()
        with contextlib.redirect_stdout(out):
            for kind, data in events:
                ui.emit(kind, **data)
        return out.getvalue()

    TOOL = ("tool", {"id": "1", "name": "run", "input": {"command": "x" * 200}})
    OUTPUT = ("output", {"line": "noisy test output"})

    def test_quiet_hides_command_output_and_shortens_tool_calls(self):
        out = self.show(False, self.TOOL, self.OUTPUT)
        self.assertNotIn("noisy test output", out)
        self.assertIn("- run", out)
        self.assertLess(len(out), 100)

    def test_verbose_shows_everything(self):
        out = self.show(True, self.TOOL, self.OUTPUT)
        self.assertIn("noisy test output", out)
        self.assertGreater(len(out), len(self.show(False, self.TOOL, self.OUTPUT)) + 60)

    def test_answer_text_and_errors_always_show(self):
        out = self.show(False, ("text", {"text": "the answer"}),
                        ("result", {"id": "1", "output": "boom", "is_error": True}))
        self.assertIn("the answer", out)
        self.assertIn("boom", out)

    def test_json_mode_is_unaffected_by_quiet(self):
        ui, out = kidwit.UI(json_mode=True), io.StringIO()
        with contextlib.redirect_stdout(out):
            ui.emit(*self.OUTPUT[:1], **self.OUTPUT[1])
        self.assertEqual(json.loads(out.getvalue()), {"type": "output", "line": "noisy test output"})


class SmarterEditTests(ToolTestCase):
    def read(self, name="f.py"):
        return (self.root / name).read_bytes().decode()

    def edit(self, old, new, name="f.py"):
        return self.call("edit_file", path=name, old_text=old, new_text=new)

    def test_trailing_whitespace_differences_are_tolerated(self):
        self.put("f.py", "def f():   \n    return 1  \n\nx = 2\n")
        out, err = self.edit("def f():\n    return 1", "def f():\n    return 2")
        self.assertFalse(err, out)
        self.assertEqual(self.read(), "def f():\n    return 2\n\nx = 2\n")

    def test_loose_match_works_the_other_way_and_with_crlf(self):
        (self.root / "f.py").write_bytes(b"a = 1\r\nb = 2\r\nc = 3\r\n")
        self.assertFalse(self.edit("b = 2   \n", "b = 20\n")[1])
        self.assertEqual(self.read(), "a = 1\r\nb = 20\r\nc = 3\r\n")

    def test_loose_match_must_be_unique(self):
        self.put("f.py", "x = 1  \ny\nx = 1\nz\n")
        out, err = self.edit("x = 1", "x = 9")
        self.assertTrue(err)
        self.assertIn("2 places", out)

    def test_exact_match_is_still_preferred_and_strict_about_indentation(self):
        self.put("f.py", "if a:\n    run()\n")
        out, err = self.edit("if a:\nrun()", "if a:\n    go()")
        self.assertTrue(err)
        self.assertEqual(self.read(), "if a:\n    run()\n")

    def test_not_found_points_at_the_closest_line(self):
        self.put("f.py", "import os\n\ndef compute_total(items):\n    return sum(items)\n")
        out, err = self.edit("def compute_totl(items):", "def compute(items):")
        self.assertTrue(err)
        self.assertIn("closest line is 3", out)
        self.assertIn("compute_total", out)

    def test_not_found_without_a_close_line_has_no_hint(self):
        self.put("f.py", "alpha\nbeta\n")
        out, err = self.edit("zzzzzzzzzz", "x")
        self.assertTrue(err)
        self.assertNotIn("closest", out)
        self.assertIn("Re-read", out)


class BadArgumentsTests(ToolTestCase):
    def test_message_names_the_tool(self):
        out, err = self.tools.call("read_file", {"path": "a", "offzet": 1})
        self.assertTrue(err)
        self.assertTrue(out.startswith("bad arguments for read_file:"), out)
        out, err = self.tools.call("edit_file", {"path": "a"})
        self.assertIn("missing", out)


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class GitAwareListingTests(ToolTestCase):
    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def setUp(self):
        super().setUp()
        self.put(".gitignore", "*.log\ngenerated/\n.env\n")
        self.put("src/app.py", "needle = 1\n")
        self.put("tracked.txt", "needle\n")
        self.git("init", "-q")
        self.git("add", "tracked.txt")
        self.put("untracked.py", "needle = 2\n")
        self.put("debug.log", "needle\n")
        self.put("generated/out.py", "needle = 3\n")
        self.put(".env", "SECRET=needle\n")

    def test_respects_gitignore_and_includes_untracked(self):
        files = self.call("list_files")[0].split("\n")
        for wanted in ("src/app.py", "tracked.txt", "untracked.py", ".gitignore"):
            self.assertIn(wanted, files)
        for hidden in ("debug.log", "generated/out.py", ".env"):
            self.assertNotIn(hidden, files)

    def test_search_skips_ignored_files(self):
        out = self.call("search", regex="needle")[0]
        self.assertIn("src/app.py:1:", out)
        for hidden in ("debug.log", "generated", ".env"):
            self.assertNotIn(hidden, out)

    def test_globs_still_work(self):
        self.assertEqual(sorted(self.call("list_files", pattern="*.py")[0].split("\n")), ["src/app.py", "untracked.py"])

    def test_deleted_tracked_file_is_not_listed(self):
        (self.root / "tracked.txt").unlink()
        self.assertNotIn("tracked.txt", self.call("list_files")[0].split("\n"))

    def test_ignored_files_can_still_be_read_explicitly(self):
        self.assertIn("SECRET", self.call("read_file", path=".env")[0])

    def test_subdirectory_of_a_repo_lists_only_its_own_files(self):
        self.tools = kidwit.Tools(self.root / "src", self.ui, "auto")
        self.assertEqual(self.call("list_files")[0], "app.py")

    def test_skip_dirs_apply_to_tracked_files_too(self):
        self.put("node_modules/lib.js", "needle\n")
        self.git("add", "-f", "node_modules/lib.js")
        self.assertNotIn("node_modules/lib.js", self.call("list_files")[0])

    def test_non_repo_falls_back_to_directory_walk(self):
        with tempfile.TemporaryDirectory() as plain:
            (Path(plain) / "a.txt").write_text("hi")
            self.assertEqual(kidwit.Tools(plain, self.ui, "auto").call("list_files", {}), ("a.txt", False))

    def test_missing_git_falls_back_to_directory_walk(self):
        with mock.patch.object(kidwit.subprocess, "run", side_effect=FileNotFoundError):
            self.assertIn("debug.log", self.call("list_files")[0].split("\n"))


class StyleTests(unittest.TestCase):
    def render(self, color, kind, **data):
        ui, out = kidwit.UI(), io.StringIO()
        ui.color = color
        with contextlib.redirect_stdout(out):
            ui.emit(kind, **data)
        return out.getvalue()

    def test_model_text_has_a_gutter_only_in_color(self):
        colored = self.render(True, "text", text="line one\nline two")
        self.assertEqual(colored.count("▌"), 2)
        self.assertIn("\033[35m", colored)
        self.assertEqual(self.render(False, "text", text="line one\nline two"), "\nline one\nline two\n")

    def test_permission_request_is_distinct(self):
        out = self.render(True, "approval", action="edit", target="a.py", detail="--- a\n+++ b\n-x\n+y\n")
        self.assertIn("permission needed: edit a.py", out)
        self.assertIn("\033[1;33m", out)
        self.assertIn("\033[32m+y", out)
        self.assertIn("\033[31m-x", out)

    def test_tool_name_is_highlighted(self):
        out = self.render(True, "tool", id="1", name="run", input={"command": "ls"})
        self.assertIn("\033[36mrun", out)
        self.assertEqual(self.render(False, "tool", id="1", name="run", input={"command": "ls"}), '- run {"command": "ls"}\n')

    def prompt_for(self, color, tty, json_mode=False):
        ui = kidwit.UI(json_mode=json_mode)
        ui.color = color
        seen = []
        with mock.patch("builtins.input", lambda p: seen.append(p) or "x"), \
                mock.patch("sys.stdin", mock.Mock(isatty=lambda: tty, readline=lambda: "x\n")):
            ui.ask(">> ", "1;32")
        return seen

    def test_prompt_is_colored_with_readline_markers_on_a_terminal(self):
        self.assertEqual(self.prompt_for(True, True), ["\001\033[1;32m\002>> \001\033[0m\002"])

    def test_prompt_is_plain_when_piped_or_without_color(self):
        self.assertEqual(self.prompt_for(True, False), [">> "])
        self.assertEqual(self.prompt_for(False, True), [">> "])
        self.assertEqual(self.prompt_for(True, True, json_mode=True), [])

    def test_repl_and_permission_prompts(self):
        asked = []
        ui = FakeUI(["y"])
        ui.ask = lambda prompt, code="0": asked.append((prompt, code)) or "y"
        tools = kidwit.Tools(tempfile.mkdtemp(), ui, "ask", log=False)
        tools.call("run", {"command": "true"})
        self.assertTrue(asked[0][0].startswith("└ allow?"))
        self.assertEqual(asked[0][1], "1;33")


class EvalSuiteTests(ProviderTestCase):
    def test_every_task_fails_as_shipped_and_passes_with_its_solution(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run_evals.selftest(TASKS), 0)

    def test_task_ids_are_unique_and_complete(self):
        ids = [t["id"] for t in TASKS]
        self.assertEqual(len(ids), len(set(ids)))
        for task in TASKS:
            self.assertTrue({"id", "prompt", "files", "verify", "solution"} <= task.keys(), task["id"])

    def run_eval(self, fake, *extra):
        out = Path(tempfile.mkdtemp()) / "results.json"
        argv = ["--task", "off-by-one", "--out", str(out), *extra, "--",
                "--provider", "openai", "--model", "m", "--base-url", fake.url]
        with contextlib.redirect_stdout(io.StringIO()) as printed, mock.patch.dict(os.environ, {**SYSTEM_ENV, "PATH": os.environ["PATH"]}, clear=True):
            code = run_evals.main(argv)
        return code, json.loads(out.read_text()), printed.getvalue()

    def reply(self, message, usage=(10, 2)):
        return (200, {"choices": [{"message": message}], "usage": {"prompt_tokens": usage[0], "completion_tokens": usage[1]}})

    def test_runner_scores_a_passing_run(self):
        edit = {"id": "c1", "type": "function", "function": {"name": "edit_file", "arguments": json.dumps(
            {"path": "calc.py", "old_text": "a - b", "new_text": "a + b"})}}
        fake = self.api(self.reply({"content": None, "tool_calls": [edit]}), self.reply({"content": "Fixed."}, (20, 3)))
        code, saved, printed = self.run_eval(fake)
        self.assertEqual(code, 0)
        result = saved["results"][0]
        self.assertTrue(result["passed"], result)
        self.assertEqual((result["turns"], result["tool_calls"], result["tool_errors"]), (2, 1, 0))
        self.assertEqual((result["input_tokens"], result["output_tokens"]), (30, 5))
        self.assertEqual((saved["passed"], saved["total"]), (1, 1))
        self.assertIn("PASS off-by-one", printed)

    def test_runner_scores_a_failing_run(self):
        fake = self.api(self.reply({"content": "I looked and changed nothing."}))
        code, saved, printed = self.run_eval(fake)
        self.assertEqual(code, 1)
        self.assertFalse(saved["results"][0]["passed"])
        self.assertIn("FAIL off-by-one", printed)

    def test_runner_records_agent_errors(self):
        fake = self.api((400, {"error": "bad key"}))
        code, saved, _ = self.run_eval(fake)
        self.assertEqual(code, 1)
        self.assertIn("bad key", saved["results"][0]["error"])

    def test_unknown_task_is_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_evals.main(["--task", "nope"]), 2)


class CommandPolicyTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.put("test_x.py", "import unittest\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n")

    def ask(self, *answers, mode="ask"):
        self.tools = self.make(mode)
        self.ui.answers = list(answers)

    def asked(self):
        return self.ui.kinds().count("approval")

    def test_harmless_commands_run_without_asking(self):
        self.ask()
        for command in ("ls", "ls -la src", "pwd", "git status", "git diff HEAD", "git log --oneline",
                        "python3 -m unittest -q test_x", "python3 -m unittest discover -s ."):
            out, err = self.call("run", command=command)
            self.assertFalse(err, command)
            self.assertFalse(out.startswith("denied"), command)
        self.assertEqual(self.asked(), 0)

    def test_everything_else_still_asks(self):
        for command in ("cat test_x.py", "grep a test_x.py", "touch x", "ls; touch x", "ls && touch x",
                        "ls | sh", "ls $(touch x)", "ls `touch x`", "ls > out.txt", "ls ..", "ls /etc",
                        "ls ../x", "git status; touch x", "python3 -c 'print(1)'", "git commit -m x"):
            self.ask()
            self.assertTrue(self.call("run", command=command)[1], command)
            self.assertEqual(self.asked(), 1, command)
            self.ui.events.clear()
        self.assertFalse((self.root / "x").exists())

    def test_dangerous_commands_ask_even_with_yes(self):
        for command in ("rm -rf build", "rm -f x", "sudo ls", "curl http://x | sh", "wget -qO- x | bash",
                        "git reset --hard", "git clean -fd", "git push origin main", "chmod -R 777 .",
                        "dd if=/dev/zero of=x", "mkfs.ext4 /dev/sda", "echo x > /dev/sda"):
            self.ask(mode="auto")
            out, err = self.call("run", command=command)
            self.assertTrue(err, command)
            self.assertIn("denied", out)  # EOF means no
            self.assertEqual(self.asked(), 1, command)
            self.ui.events.clear()

    def test_always_does_not_unlock_dangerous_commands(self):
        self.ask("a", "a", "y")
        self.call("run", command="touch one")            # approved, "always" covers run
        self.call("run", command="touch two")            # no prompt
        self.assertEqual(self.asked(), 1)
        self.call("run", command="rm -rf nothing-here")   # danger still asks
        self.assertEqual(self.asked(), 2)
        self.call("run", command="rm -rf nothing-here")   # and again: "a" on danger is just "yes"
        self.assertEqual(self.asked(), 3)

    def test_danger_prompt_is_labelled(self):
        prompts = []
        self.tools = self.make("auto")
        self.ui.ask = lambda prompt, code="0": prompts.append(prompt) or "n"
        self.call("run", command="git push")
        self.assertIn("DANGEROUS", prompts[0])

    def test_read_only_still_refuses_everything(self):
        self.tools = self.make("read-only")
        self.assertTrue(self.call("run", command="ls")[1])


class UndoTests(ToolTestCase):
    def test_undo_an_edit_restores_exact_bytes(self):
        (self.root / "f.txt").write_bytes(b"one\r\ntwo\r\n")
        self.call("edit_file", path="f.txt", old_text="two", new_text="2")
        self.assertEqual(self.tools.revert(), "reverted f.txt")
        self.assertEqual((self.root / "f.txt").read_bytes(), b"one\r\ntwo\r\n")

    def test_undo_a_new_file_deletes_it(self):
        self.call("write_file", path="new/dir/a.txt", content="x")
        self.assertEqual(self.tools.revert(), "reverted new/dir/a.txt")
        self.assertFalse((self.root / "new/dir/a.txt").exists())

    def test_undo_an_overwrite(self):
        self.put("a.txt", "old")
        self.call("write_file", path="a.txt", content="new")
        self.tools.revert()
        self.assertEqual((self.root / "a.txt").read_text(), "old")

    def test_undo_steps_back_through_several_changes(self):
        self.put("a.txt", "0")
        for n in "123":
            self.call("write_file", path="a.txt", content=n)
        for expected in ("2", "1", "0"):
            self.tools.revert()
            self.assertEqual((self.root / "a.txt").read_text(), expected)
        self.assertEqual(self.tools.revert(), "nothing to undo")

    def test_denied_changes_leave_nothing_to_undo(self):
        self.tools = self.make("ask")
        self.call("write_file", path="a.txt", content="x")  # no answer scripted: EOF means no
        self.assertEqual(self.tools.revert(), "nothing to undo")


class CutOffReplyTests(ProviderTestCase):
    def test_anthropic_cut_off_tool_call_is_not_run(self):
        reply = {"stop_reason": "max_tokens", "content": [
            {"type": "tool_use", "id": "t1", "name": "write_file", "input": {"path": "a.py", "content": "half"}}]}
        fake = self.api((200, reply))
        _, calls, _ = kidwit.AnthropicProvider("k", "m", fake.url).chat("s", HISTORY[:1], kidwit.TOOLS)
        self.assertEqual(calls[0]["name"], "write_file")
        out, err = kidwit.Tools(".", None, "auto").call(calls[0]["name"], calls[0]["input"])
        self.assertTrue(err)
        self.assertIn("cut off", out)

    def test_openai_cut_off_tool_call_is_not_run(self):
        call = {"id": "x1", "type": "function", "function": {"name": "run", "arguments": '{"command": "rm'}}
        fake = self.api((200, {"choices": [{"finish_reason": "length", "message": {"content": "", "tool_calls": [call]}}]}))
        _, calls, _ = kidwit.OpenAIProvider("k", "m", fake.url).chat("s", HISTORY[:1], kidwit.TOOLS)
        self.assertIn("_error", calls[0]["input"])

    def test_normal_stop_reasons_are_untouched(self):
        fake = self.api((200, {"stop_reason": "tool_use", "content": [
            {"type": "tool_use", "id": "t1", "name": "run", "input": {"command": "ls"}}]}))
        _, calls, _ = kidwit.AnthropicProvider("k", "m", fake.url).chat("s", HISTORY[:1], kidwit.TOOLS)
        self.assertEqual(calls[0]["input"], {"command": "ls"})


class CompactionTests(AgentTestCase):
    def big_history(self, pairs=12, text_size=30000):
        h = [{"role": "user", "text": "original task: fix the parser"}]
        for i in range(pairs):
            h.append({"role": "assistant", "text": "t" * text_size, "calls": [call(str(i), "read_file", path=f"f{i}.py")]})
            h.append({"role": "tool", "results": [{"id": str(i), "output": "out" * 200, "is_error": i == 3}]})
        return h

    def test_under_budget_makes_no_summary_call(self):
        bot = self.agent(("done", [], (1, 1)))
        bot.history = self.big_history(pairs=2, text_size=100)
        bot.run("go")
        self.assertEqual(len(self.provider.seen), 1)
        self.assertEqual(self.provider.seen[0][0], "SYSTEM")

    def test_over_budget_replaces_the_older_half_with_a_summary(self):
        bot = self.agent(("SUMMARY: fixing the parser; read f0..f5", [], (1, 1)), ("all done", [], (1, 1)))
        bot.history = self.big_history()
        before = len(bot.history)
        self.assertEqual(bot.run("continue"), "all done")
        system, summary_request, tools = self.provider.seen[0]
        self.assertEqual((system, tools), (kidwit.SUMMARY_SYSTEM, []))
        self.assertEqual([m["role"] for m in summary_request], ["user"])
        self.assertIn("original task: fix the parser", summary_request[0]["text"])
        self.assertIn("CALL read_file", summary_request[0]["text"])
        self.assertIn("RESULT (error)", summary_request[0]["text"])
        main_call = self.provider.seen[1][1]
        self.assertTrue(main_call[0]["text"].startswith("Summary of the earlier work"))
        self.assertIn("fixing the parser", main_call[0]["text"])
        self.assertLess(len(main_call), before)
        self.assertEqual(main_call[-1], {"role": "user", "text": "continue"})
        self.assertIn("summarized", self.ui.events[0][1]["message"])

    def test_cut_never_separates_a_tool_call_from_its_result(self):
        for pairs in range(3, 16):
            bot = self.agent(("S", [], (1, 1)))
            bot.history = self.big_history(pairs=pairs, text_size=40000)
            bot.compact()
            assert_valid_history(self, bot.history)
            self.assertEqual(bot.history[0]["role"], "user")
            self.assertEqual(bot.history[1]["role"], "assistant")
            for a, b in zip(bot.history, bot.history[1:]):
                self.assertFalse(a["role"] == b["role"] == "assistant")

    def test_summary_failure_keeps_the_history_and_the_task_continues(self):
        bot = self.agent(kidwit.AgentError("summary API down"), ("carried on", [], (1, 1)))
        bot.history = self.big_history()
        before = len(bot.history)
        self.assertEqual(bot.run("go"), "carried on")
        self.assertEqual(len(bot.history), before + 2)  # no message dropped: the task and the final answer were added
        self.assertEqual(bot.history[0]["text"], "original task: fix the parser")

    def test_empty_summary_is_ignored(self):
        bot = self.agent(("   ", [], (1, 1)))
        bot.history = self.big_history()
        before = len(bot.history)
        bot.compact()
        self.assertEqual(len(bot.history), before)

    def test_tiny_histories_are_left_alone(self):
        bot = self.agent()
        bot.history = [{"role": "user", "text": "x" * 500000}]
        bot.compact()
        self.assertEqual(len(bot.history), 1)
        self.assertEqual(self.provider.seen, [])

    def test_transcript_cuts_long_output_and_marks_errors(self):
        text = kidwit.transcript([
            {"role": "user", "text": "task"},
            {"role": "assistant", "text": "thinking", "calls": [call("1", "run", command="x" * 500)]},
            {"role": "tool", "results": [{"id": "1", "output": "y" * 5000, "is_error": True}]}])
        self.assertIn("USER: task", text)
        self.assertIn("CALL run", text)
        self.assertIn("RESULT (error)", text)
        self.assertLess(len(text), 1000)

    def test_trim_reports_the_size(self):
        bot = self.agent()
        bot.history = self.big_history(pairs=2, text_size=100)
        self.assertEqual(bot.trim(), len(json.dumps(bot.history)))


class NoEmptyToolsTests(ProviderTestCase):
    def test_requests_without_tools_omit_the_field(self):
        fake = self.api((200, {"content": [{"type": "text", "text": "ok"}]}),
                        (200, {"choices": [{"message": {"content": "ok"}}]}))
        kidwit.AnthropicProvider("k", "m", fake.url).chat("s", HISTORY[:1], [])
        kidwit.OpenAIProvider("k", "m", fake.url).chat("s", HISTORY[:1], [])
        self.assertTrue(all("tools" not in r["body"] for r in fake.requests))


if __name__ == "__main__":
    unittest.main()
