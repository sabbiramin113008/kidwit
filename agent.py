#!/usr/bin/env python3
"""kidwit: a minimal coding-agent engine. Standard library only.

Usage: python3 agent.py "fix the failing test"   (no task: REPL; --json: JSON-lines events)
"""
import argparse
import difflib
import json
import os
import platform
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

VERSION = "0.1.0"
MAX_TURNS = 50
MAX_TOKENS = 8192
MAX_TOOL_OUTPUT = 12000
CONTEXT_BUDGET = 200000
DEFAULT_TIMEOUT = 120
DEFAULT_MODEL = "claude-sonnet-5-5"
BACKOFF = 2
RETRY_CODES = {408, 429, 500, 502, 503, 504, 529}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".agent", ".next", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache"}
OLD_OUTPUT = "[old output removed to save context]"
SUMMARY_SYSTEM = ("You summarize a coding agent's earlier work so it can continue after the old messages are dropped. "
                  "Write compact plain text: the user's task and later instructions, files read or changed and why, "
                  "commands run and their results, decisions made, and what is still left to do. Keep exact file "
                  "paths, names and error messages. No preamble.")
# Commands that run without asking in ask mode: read-only inspection and test runners. Deliberately
# no cat/grep/head: the path jail covers the file tools, not `run`, so those could read outside the root.
CUT_OFF = {"_error": "your reply hit the output token limit and this call was cut off, so it did not run; "
                     "do less per call, for example edit_file instead of rewriting a whole file"}
SAFE_CMD = re.compile(r"(ls|pwd|git (status|diff|log|show|branch)|python3? -m (unittest|pytest)|pytest"
                      r"|npm test|go test|cargo test)( [\w:./-]+)*")
# Commands that always ask, even with --yes or after "always".
DANGER = re.compile(r"\brm\s+-\w*[rf]|\bsudo\b|\bmkfs|\bdd\s+if=|\bch(mod|own)\s+-\w*R|\|\s*(ba|z)?sh\b"
                    r"|>\s*/dev/|\bgit\s+(reset\s+--hard|clean|push)\b")
BANNER = "  █▄▀ █ █▀▄ █ █ █ █ ▀█▀\n  █ █ █ █▄▀ ▀▄▀▄▀ █  █ "

SYSTEM = """You are kidwit, a coding agent working inside one project directory.
Project root: {root}   OS: {os}   Shell: {shell}

Explore first: use list_files and search to find the right files, and read_file before you change anything.
Make the smallest change that solves the task. Prefer edit_file, copying old_text exactly with enough context to match once.
Never guess file contents or paths. If a tool call fails, read the error and fix the call instead of repeating it.
After changing code, run the tests or a relevant command to check your work, and fix anything you broke.
Paths are relative to the project root. If the user denies an action, adapt to their feedback and do not retry it.
If the request is ambiguous or risky, ask a short question instead of guessing.
Be concise. When done, say what you changed and the result."""


class AgentError(Exception):
    """Fatal problem for the current task (API failure, bad configuration)."""


class ToolError(Exception):
    """A tool failure the model should read and recover from."""


def clip(text, limit=MAX_TOOL_OUTPUT):
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return f"{text[:head]}\n...[{len(text) - limit} chars clipped]...\n{text[-(limit - head):]}"


def _tool(name, desc, props, required=()):
    schema = {k: {"type": t} for k, t in props.items()}
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": schema, "required": list(required)}}


GLOB = "pattern is an optional glob such as '*.py' or 'src/**/*.py'."
TOOLS = [
    _tool("list_files", f"List project files. {GLOB}", {"pattern": "string"}),
    _tool("search", f"Search file contents with a Python regex; returns path:line: text. {GLOB}",
          {"regex": "string", "pattern": "string"}, ["regex"]),
    _tool("read_file", "Read a file with line numbers. offset is the 1-based first line; "
          "limit defaults to 2000.", {"path": "string", "offset": "integer", "limit": "integer"}, ["path"]),
    _tool("write_file", "Create or overwrite a file.",
          {"path": "string", "content": "string"}, ["path", "content"]),
    _tool("edit_file", "Replace exact text in a file. old_text must match exactly once "
          "unless replace_all is true.",
          {"path": "string", "old_text": "string", "new_text": "string", "replace_all": "boolean"},
          ["path", "old_text", "new_text"]),
    _tool("run", "Run a shell command in the project root. timeout is in seconds (default 120).",
          {"command": "string", "timeout": "integer"}, ["command"]),
]
WRITE_TOOLS = {"write_file", "edit_file", "run"}


def post_json(url, headers, body):
    """POST JSON and return the parsed reply. Retries transient failures with backoff."""
    data, err = json.dumps(body).encode(), "unknown error"
    for attempt in range(4):
        req = urllib.request.Request(url, data, {"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            with e:
                detail = e.read().decode("utf-8", errors="replace")[:1500]
            if e.code not in RETRY_CODES:
                raise AgentError(f"HTTP {e.code}: {detail}")
            err = f"HTTP {e.code}: {detail}"
        except ValueError:
            raise AgentError("the API returned a reply that is not valid JSON")
        except OSError as e:
            err = str(e)
        if attempt < 3:
            time.sleep(BACKOFF * 2 ** attempt)
    raise AgentError(f"request failed after 4 attempts: {err}")


class Provider:
    """chat(system, history, tools) -> (text, calls, (tokens_in, tokens_out)). PROJECT.md section 5."""

    def __init__(self, key, model, base=None):
        self.key, self.model, self.base = key, model, (base or self.default_base).rstrip("/")


class AnthropicProvider(Provider):
    default_base = "https://api.anthropic.com"

    @staticmethod
    def convert(history):
        """Neutral history -> API messages, merging consecutive same-role messages."""
        msgs = []
        for m in history:
            if m["role"] == "user":
                role, blocks = "user", [{"type": "text", "text": m["text"]}]
            elif m["role"] == "assistant":
                role = "assistant"
                blocks = [{"type": "text", "text": m["text"]}] if m["text"] else []
                blocks += [{"type": "tool_use", **c} for c in m.get("calls", [])]
            else:
                role = "user"
                blocks = [{"type": "tool_result", "tool_use_id": r["id"], "content": r["output"],
                           "is_error": r["is_error"]} for r in m["results"]]
            if msgs and msgs[-1]["role"] == role:
                msgs[-1]["content"] += blocks
            else:
                msgs.append({"role": role, "content": blocks})
        return msgs

    def chat(self, system, history, tools):
        cache = {"type": "ephemeral"}
        msgs = self.convert(history)
        if msgs:
            msgs[-1]["content"][-1]["cache_control"] = cache
        body = {"model": self.model, "max_tokens": MAX_TOKENS, "messages": msgs,
                "system": [{"type": "text", "text": system, "cache_control": cache}]}
        if tools:
            body["tools"] = tools
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
        reply = post_json(f"{self.base}/v1/messages", headers, body)
        blocks = reply.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        calls = [{"id": b["id"], "name": b["name"], "input": b.get("input") or {}}
                 for b in blocks if b.get("type") == "tool_use"]
        if reply.get("stop_reason") == "max_tokens":
            calls = [{**c, "input": CUT_OFF} for c in calls]
        u = reply.get("usage") or {}
        tokens_in = sum(u.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens",
                                              "cache_creation_input_tokens"))
        return text, calls, (tokens_in, u.get("output_tokens", 0))


class OpenAIProvider(Provider):
    """OpenAI-compatible Chat Completions (OpenAI, OpenRouter, Ollama, LM Studio, vLLM)."""
    default_base = "https://api.openai.com/v1"

    @staticmethod
    def convert(system, history):
        msgs = [{"role": "system", "content": system}]
        for m in history:
            if m["role"] == "user":
                msgs.append({"role": "user", "content": m["text"]})
            elif m["role"] == "assistant":
                calls = m.get("calls") or []
                msg = {"role": "assistant", "content": m["text"] or (None if calls else "")}
                if calls:
                    msg["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                        "name": c["name"], "arguments": json.dumps(c["input"])}} for c in calls]
                msgs.append(msg)
            else:
                msgs += [{"role": "tool", "tool_call_id": r["id"], "content": r["output"]}
                         for r in m["results"]]
        return msgs

    def chat(self, system, history, tools):
        body = {"model": self.model, "messages": self.convert(system, history)}
        if tools:
            body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                               "parameters": t["input_schema"]}} for t in tools]
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        reply = post_json(f"{self.base}/chat/completions", headers, body)
        try:
            choice = reply["choices"][0]
            msg = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise AgentError(f"unexpected API reply: {str(reply)[:500]}")
        calls = []
        for c in msg.get("tool_calls") or []:
            raw = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except ValueError:
                args = None
            calls.append({"id": c["id"], "name": c["function"]["name"],
                          "input": args if isinstance(args, dict) else {"_invalid_json": raw}})
        if choice.get("finish_reason") == "length":
            calls = [{**c, "input": CUT_OFF} for c in calls]
        u = reply.get("usage") or {}
        return msg.get("content") or "", calls, (u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def glob_re(pat):
    """Glob to regex: '*' and '?' stay inside a directory; '**/' also matches zero directories."""
    rx = re.escape(pat).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
    return re.compile(rx.replace(r"\*", "[^/]*").replace(r"\?", "[^/]") + r"\Z")


def kill_tree(proc):
    """Kill a command and its children (a new session makes the process group killable)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL) if os.name == "posix" else proc.kill()
    except OSError:
        pass


def loose_matches(text, old):
    """Slices of text equal to old once trailing whitespace is ignored on every line."""
    lines, want = text.splitlines(True), [line.rstrip() for line in old.splitlines()]
    spans = ["".join(lines[i:i + len(want)]) for i in range(len(lines) - len(want) + 1)
             if [line.rstrip() for line in lines[i:i + len(want)]] == want]
    return spans if old.endswith("\n") else [span.rstrip("\r\n") for span in spans]


def diff(old, new, name):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"a/{name}", f"b/{name}"))


class Tools:
    def __init__(self, root, ui, mode="ask", log=True):
        self.root, self.ui, self.mode, self.log = Path(root).resolve(), ui, mode, log
        self.always, self.undo = set(), []  # kinds approved with "always"; (path, old bytes) per change

    def available(self):
        return [t for t in TOOLS if self.mode != "read-only" or t["name"] not in WRITE_TOOLS]

    def call(self, name, args):
        """Run a tool. Never raises: every failure becomes (message, True)."""
        if self.mode == "read-only" and name in WRITE_TOOLS:
            return f"{name} is disabled in read-only mode", True
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            return f"unknown tool: {name}", True
        try:
            if not isinstance(args, dict):
                raise TypeError("arguments must be an object")
            if "_invalid_json" in args:
                raise ToolError("bad arguments: not valid JSON: " + str(args["_invalid_json"])[:200])
            if "_error" in args:
                raise ToolError(args["_error"])
            return clip(fn(**args)), False
        except ToolError as e:
            return clip(str(e)), True
        except TypeError as e:
            return f"bad arguments for {name}: {e}", True
        except Exception as e:
            return f"{type(e).__name__}: {e}", True

    def path(self, p):
        """Resolve p inside the project root. Symlinks are followed, then checked."""
        full = (self.root / p).resolve()
        if full != self.root and self.root not in full.parents:
            raise ToolError(f"path is outside the project root: {p}")
        return full

    def rel(self, p):
        return p.relative_to(self.root).as_posix()

    def listing(self):
        """Project files: git's list (honours .gitignore) inside a repo, else a directory walk."""
        try:
            raw = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=self.root,
                                 capture_output=True, check=True, timeout=10).stdout.decode("utf-8", "replace")
            if raw:
                return sorted(self.root / name for name in raw.split("\0") if name)
        except (OSError, subprocess.SubprocessError):
            pass
        paths = []
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            paths += [Path(dirpath) / name for name in sorted(files)]
        return paths

    def walk(self, pattern=None):
        """Yield (path, relpath) for project files, skipping SKIP_DIRS and outside symlinks."""
        rx = glob_re(pattern) if pattern else None
        for p in self.listing():
            rel = self.rel(p)
            outside = p.is_symlink() and self.root not in p.resolve().parents
            if p.is_file() and not outside and not SKIP_DIRS.intersection(rel.split("/")[:-1]) \
                    and (rx is None or rx.match(rel if "/" in pattern else p.name)):
                yield p, rel

    def load(self, p):
        data = p.read_bytes()
        if b"\0" in data[:4096]:
            raise ToolError(f"binary file: {self.rel(p)}")
        return data.decode("utf-8", errors="replace")

    def t_list_files(self, pattern=None):
        rels = [rel for _, rel in self.walk(pattern)]
        more = f"\n[truncated: {len(rels) - 1000} more files]" if len(rels) > 1000 else ""
        return "\n".join(rels[:1000]) + more or "(no files)"

    def t_search(self, regex, pattern=None):
        try:
            rx = re.compile(regex)
        except re.error as e:
            raise ToolError(f"invalid regex: {e}")
        hits = []
        for p, rel in self.walk(pattern):
            try:
                text = self.load(p) if p.stat().st_size <= 2_000_000 else ""
            except (OSError, ToolError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{n}: {line[:300]}")
                    if len(hits) >= 200:
                        return "\n".join(hits) + "\n[stopped at 200 matches]"
        return "\n".join(hits) or "no matches"

    def t_read_file(self, path, offset=1, limit=2000):
        lines = self.load(self.path(path)).splitlines()
        offset, limit = max(1, int(offset)), int(limit)
        chunk = lines[offset - 1:offset - 1 + limit]
        out = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(chunk, offset))
        left = len(lines) - (offset - 1 + len(chunk))
        if left > 0:
            out += f"\n[{left} more lines; use offset={offset + len(chunk)}]"
        return out or "(empty file, or offset is past the end)"

    def confirm(self, action, target, detail, force=False):
        """Ask before a write, edit or command. Raises ToolError if denied. force asks even in auto mode."""
        kind = "run" if action == "run" else "files"  # "always" covers file changes or commands, not both
        if not force and (self.mode == "auto" or kind in self.always):
            return
        self.ui.emit("approval", action=action, target=target, detail=detail)
        prompt = "└ DANGEROUS, allow?" if force else "└ allow?"
        answer = (self.ui.ask(f"{prompt} [y]es / [a]lways / [n]o / or type feedback › ", "1;33") or "").strip()
        low = answer.lower()
        if low in ("a", "always"):
            if not force:
                self.always.add(kind)
        elif low not in ("y", "yes"):
            raise ToolError("denied by user" + (f": {answer}" if low not in ("", "n", "no") else ""))

    def audit(self, action, target):
        """Append a JSON line to .agent/audit.jsonl. Failures never break the agent."""
        if not self.log:
            return
        try:
            folder = self.root / ".agent"
            folder.mkdir(exist_ok=True)
            (folder / ".gitignore").write_text("*\n")
            entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action, "target": target}
            with (folder / "audit.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass

    def save(self, p, text):
        self.undo.append((p, p.read_bytes() if p.exists() else None))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))  # bytes keep line endings exactly

    def revert(self):
        """Undo the most recent write or edit. Commands cannot be undone."""
        if not self.undo:
            return "nothing to undo"
        p, old = self.undo.pop()
        p.unlink() if old is None else p.write_bytes(old)
        return f"reverted {self.rel(p)}"

    def t_write_file(self, path, content):
        p = self.path(path)
        rel = self.rel(p)
        old = self.load(p) if p.exists() else None
        detail = diff(old, content, rel) if old is not None else content[:2000]
        self.confirm("write", rel, detail)
        self.audit("write", rel)
        self.save(p, content)
        return f"wrote {len(content)} chars to {rel}"

    def t_edit_file(self, path, old_text, new_text, replace_all=False):
        p = self.path(path)
        rel, text = self.rel(p), self.load(p)
        if not old_text:
            raise ToolError("old_text must not be empty")
        if old_text not in text and "\r\n" in text:
            old_text = old_text.replace("\r\n", "\n").replace("\n", "\r\n")
            new_text = new_text.replace("\r\n", "\n").replace("\n", "\r\n")
        count = text.count(old_text)
        if count == 0 and len(spans := loose_matches(text, old_text)) == 1:
            old_text, count = spans[0], 1
        if count == 0:
            lines = [line.strip() for line in text.splitlines()]
            near = difflib.get_close_matches(old_text.strip().split("\n")[0].strip(), lines, 1, 0.6)
            hint = f"; the closest line is {lines.index(near[0]) + 1}: {near[0]!r}" if near else ""
            raise ToolError(f"old_text not found in {rel}{hint}. Re-read the file and copy it exactly")
        if count > 1 and not replace_all:
            raise ToolError(f"old_text matches {count} places in {rel}; add surrounding "
                            "context to make it unique, or set replace_all")
        new = text.replace(old_text, new_text, -1 if replace_all else 1)
        self.confirm("edit", rel, diff(text, new, rel))
        self.audit("edit", rel)
        self.save(p, new)
        return f"edited {rel} ({count if replace_all else 1} replacement(s))"

    def t_run(self, command, timeout=DEFAULT_TIMEOUT):
        timeout = float(timeout)
        danger = bool(DANGER.search(command))
        if danger or not (SAFE_CMD.fullmatch(command) and ".." not in command and " /" not in command):
            self.confirm("run", command, command, force=danger)
        self.audit("run", command)
        proc = subprocess.Popen(command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8",
                                errors="replace", start_new_session=os.name == "posix",
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})  # no stale .pyc after fast edits
        expired = []
        timer = threading.Timer(timeout, lambda: (expired.append(True), kill_tree(proc)))
        timer.start()
        lines = []
        try:
            for line in proc.stdout:
                lines.append(line)
                self.ui.emit("output", line=line.rstrip("\n"))
            proc.wait()
        except BaseException:
            kill_tree(proc)
            raise
        finally:
            timer.cancel()
            proc.stdout.close()
        status = f"[timed out after {timeout:g}s and was killed]" if expired else f"[exit code {proc.returncode}]"
        return status + "\n" + "".join(lines)


class UI:
    """Terminal output, or JSON-lines events with --json (PROJECT.md section 6.4)."""

    def __init__(self, json_mode=False, verbose=False):
        self.json, self.verbose = json_mode, verbose
        self.color = not json_mode and sys.stdout.isatty() and "NO_COLOR" not in os.environ

    def paint(self, code, text):
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def paint_diff(self, text):
        codes = {"+": "32", "-": "31", "@": "36"}
        return "\n".join(self.paint(codes.get(line[:1], "0"), line) for line in text.splitlines())

    def banner(self):
        if self.color:
            print("\n".join(self.paint(f"1;38;5;{c}", row) for c, row in zip((205, 141), BANNER.split("\n"))))

    def emit(self, kind, **d):
        if self.json:
            print(json.dumps({"type": kind, **d}), flush=True)
        elif kind == "text":
            print("\n" + "\n".join(self.paint("35", "▌ ") + line for line in d["text"].splitlines())
                  if self.color else "\n" + d["text"])
        elif kind == "info":
            print(self.paint("2", d["message"]))
        elif kind == "tool":
            args = json.dumps(d["input"])[:120 if self.verbose else 60]
            print(self.paint("2", "- ") + self.paint("36", d["name"]) + self.paint("2", " " + args))
        elif kind == "output" and self.verbose:
            print(self.paint("2", "  " + d["line"]), flush=True)
        elif kind == "approval":
            print(self.paint("1;33", f"┌ permission needed: {d['action']} {d['target']}"))
            print("  " + d["detail"] if d["action"] == "run" else self.paint_diff(d["detail"]))
        elif kind == "result" and d["is_error"]:
            print(self.paint("31", "x " + d["output"].split("\n")[0][:200]))
        elif kind == "done":
            print(self.paint("2", "done: {turns} turns, {input_tokens} in / {output_tokens} out tokens".format(**d)))
        elif kind == "error":
            print(self.paint("31", "error: " + d["message"]), file=sys.stderr)

    def ask(self, prompt, code="0"):
        """Read one line from the user. Returns None on EOF."""
        if self.json:
            line = sys.stdin.readline()
            return line.rstrip("\n") if line else None
        if self.color and sys.stdin.isatty():  # \001..\002 tell readline the escape codes take no width
            prompt = f"\001\033[{code}m\002{prompt}\001\033[0m\002"
        try:
            return input(prompt)
        except EOFError:
            return None


def transcript(messages):
    """Plain-text rendering of history, with long text and outputs cut, for the summarizer."""
    lines = []
    for m in messages:
        if m["role"] == "user":
            lines.append("USER: " + m["text"][:4000])
        elif m["role"] == "assistant":
            calls = "".join(f"\n  CALL {c['name']} {json.dumps(c['input'])[:200]}" for c in m.get("calls", []))
            lines.append("ASSISTANT: " + m["text"][:1500] + calls)
        else:
            lines += [f"  RESULT{' (error)' if r['is_error'] else ''}: {r['output'][:300]}" for r in m["results"]]
    return "\n".join(lines)[:60000]


class Agent:
    def __init__(self, provider, tools, ui, system):
        self.provider, self.tools, self.ui, self.system = provider, tools, ui, system
        self.history = []

    def run(self, task):
        """Run one task to completion. Returns the final text, or None on error/interrupt."""
        self.history.append({"role": "user", "text": task})
        tokens_in = tokens_out = 0
        try:
            for turn in range(1, MAX_TURNS + 1):
                if self.trim() > CONTEXT_BUDGET:
                    self.compact()
                text, calls, (n_in, n_out) = self.provider.chat(
                    self.system, self.history, self.tools.available())
                tokens_in, tokens_out = tokens_in + n_in, tokens_out + n_out
                self.history.append({"role": "assistant", "calls": calls,
                                     "text": text or ("" if calls else "(empty response)")})
                if text:
                    self.ui.emit("text", text=text)
                if not calls:
                    self.ui.emit("done", turns=turn, input_tokens=tokens_in, output_tokens=tokens_out)
                    return text
                results = []
                for c in calls:
                    self.ui.emit("tool", id=c["id"], name=c["name"], input=c["input"])
                    output, is_error = self.tools.call(c["name"], c["input"])
                    self.ui.emit("result", id=c["id"], output=output, is_error=is_error)
                    results.append({"id": c["id"], "output": output, "is_error": is_error})
                self.history.append({"role": "tool", "results": results})
            self.ui.emit("error", message=f"stopped after {MAX_TURNS} turns without finishing")
        except (KeyboardInterrupt, AgentError) as e:
            self.repair()
            self.ui.emit("error", message=str(e) or "interrupted")
        return None

    def repair(self):
        """Give every tool call a result, or the next API request is rejected."""
        last = self.history[-1] if self.history else {}
        if last.get("role") == "assistant" and last.get("calls"):
            self.history.append({"role": "tool", "results": [
                {"id": c["id"], "output": "interrupted before this tool ran", "is_error": True}
                for c in last["calls"]]})

    def trim(self):
        """Drop old tool output when the history outgrows CONTEXT_BUDGET. Last two messages stay. Returns the size."""
        size = len(json.dumps(self.history))
        for m in self.history[:-2]:
            for r in m.get("results", []):
                if size <= CONTEXT_BUDGET:
                    return size
                if len(r["output"]) > 200:
                    size -= len(r["output"]) - len(OLD_OUTPUT)
                    r["output"] = OLD_OUTPUT
        return size

    def compact(self):
        """Still over budget after trim(): replace the older half of the history with a model-written summary."""
        cut = next((i for i in range(max(1, len(self.history) // 2), len(self.history) - 2)
                    if self.history[i]["role"] == "assistant"), None)
        if cut is None:
            return
        try:
            summary = self.provider.chat(SUMMARY_SYSTEM, [{"role": "user", "text": transcript(self.history[:cut])}], [])[0]
        except AgentError:
            return  # keep going with the trimmed history
        if summary.strip():
            self.history[:cut] = [{"role": "user", "text": "Summary of the earlier work in this session:\n" + summary}]
            self.ui.emit("info", message="summarized the earlier conversation to save context")


def build_system(root):
    """System prompt plus the project's AGENTS.md or CLAUDE.md, if present."""
    system = SYSTEM.format(root=root, os=f"{platform.system()} {platform.release()}",
                           shell=os.environ.get("SHELL") or os.environ.get("COMSPEC") or "sh")
    for name in ("AGENTS.md", "CLAUDE.md"):
        if (root / name).is_file():
            text = (root / name).read_text(encoding="utf-8", errors="replace")
            return f"{system}\n\nProject instructions from {name}:\n{text[:20000]}"
    return system


def make_provider(args):
    """Pick a provider: --provider, then AGENT_PROVIDER, then Anthropic if its key is set."""
    env = os.environ
    name = args.provider or env.get("AGENT_PROVIDER") or ("anthropic" if env.get("ANTHROPIC_API_KEY") else "openai")
    model, base = args.model or env.get("AGENT_MODEL"), args.base_url or env.get("AGENT_BASE_URL")
    if name == "anthropic":
        if not env.get("ANTHROPIC_API_KEY"):
            raise AgentError("ANTHROPIC_API_KEY is not set")
        return AnthropicProvider(env["ANTHROPIC_API_KEY"], model or DEFAULT_MODEL, base)
    if name != "openai":
        raise AgentError(f"unknown provider: {name} (use anthropic or openai)")
    if not env.get("OPENAI_API_KEY") and not base:
        raise AgentError("no provider configured: set ANTHROPIC_API_KEY, or OPENAI_API_KEY, "
                         "or AGENT_BASE_URL for a local server")
    if not model:
        raise AgentError("set AGENT_MODEL or --model for the OpenAI-compatible provider")
    return OpenAIProvider(env.get("OPENAI_API_KEY"), model, base)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="kidwit", description="A minimal coding agent.")
    ap.add_argument("task", nargs="*", help="task to run once; omit for an interactive session")
    ap.add_argument("--root", default=".", help="project directory (default: current)")
    ap.add_argument("--provider", choices=["anthropic", "openai"])
    ap.add_argument("--model")
    ap.add_argument("--base-url")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--yes", action="store_true", help="never ask for approval")
    mode.add_argument("--read-only", action="store_true", help="no writes or commands")
    ap.add_argument("--json", action="store_true", help="emit JSON-lines events")
    ap.add_argument("--verbose", action="store_true", help="show live command output and full tool calls")
    ap.add_argument("--no-log", action="store_true", help="disable .agent/audit.jsonl")
    ap.add_argument("--version", action="version", version=f"kidwit {VERSION}")
    args = ap.parse_args(argv)
    ui, root = UI(args.json, args.verbose), Path(args.root).resolve()
    try:
        if not root.is_dir():
            raise AgentError(f"not a directory: {root}")
        provider = make_provider(args)
    except AgentError as e:
        ui.emit("error", message=str(e))
        return 1
    tools = Tools(root, ui, "auto" if args.yes else "read-only" if args.read_only else "ask", not args.no_log)
    bot = Agent(provider, tools, ui, build_system(root))
    if args.task:
        return 0 if bot.run(" ".join(args.task)) is not None else 1
    ui.banner()
    ui.emit("info", message=f"kidwit {VERSION} in {root}. /clear resets, /undo reverts the last edit, /verbose toggles detail, /exit quits.")
    while True:
        try:
            line = ui.ask(">> ", "1;32")
        except KeyboardInterrupt:
            return 0
        if line is None or line.strip() in ("/exit", "/quit"):
            return 0
        if line.strip() == "/clear":
            bot.history.clear()
            ui.emit("info", message="history cleared")
        elif line.strip() == "/undo":
            note = tools.revert()
            bot.history.append({"role": "user", "text": f"[the user ran /undo: {note}]"})
            ui.emit("info", message=note)
        elif line.strip() == "/verbose":
            ui.verbose = not ui.verbose
            ui.emit("info", message="verbose " + ("on" if ui.verbose else "off"))
        elif line.strip():
            bot.run(line)


if __name__ == "__main__":
    sys.exit(main())
