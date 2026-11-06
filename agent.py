#!/usr/bin/env python3
"""kidwit: a minimal coding-agent engine. Standard library only.

Usage:
  python3 agent.py "fix the failing test"   # one-shot
  python3 agent.py                          # interactive REPL
  python3 agent.py --json "task"            # JSON-lines events on stdout
Env: ANTHROPIC_API_KEY, OPENAI_API_KEY, AGENT_PROVIDER, AGENT_MODEL, AGENT_BASE_URL
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
CONTEXT_BUDGET = 400000
DEFAULT_TIMEOUT = 120
DEFAULT_MODEL = "claude-sonnet-5-5"
BACKOFF = 2
RETRY_CODES = {408, 429, 500, 502, 503, 504, 529}
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
             ".agent", ".next", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache"}
OLD_OUTPUT = "[old output removed to save context]"

SYSTEM = """You are kidwit, a coding agent working inside one project directory.
Project root: {root}
OS: {os}   Shell: {shell}

Work like a careful engineer:
- Explore with list_files and search, then read_file before you change anything.
- Prefer edit_file for small changes; copy old_text exactly from the file.
- Use run to execute tests and commands. Verify your work by running the tests.
- Paths are relative to the project root. You cannot touch files outside it.
- The user may deny an action and give feedback; adapt and try another way.
- Be concise. When the task is done, say what you changed and the result."""


class AgentError(Exception):
    """Fatal problem for the current task (API failure, bad configuration)."""


class ToolError(Exception):
    """A tool failure the model should read and recover from."""


def clip(text, limit=MAX_TOOL_OUTPUT):
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return f"{text[:head]}\n...[{len(text) - limit} chars clipped]...\n{text[-(limit - head):]}"


def _tool(name, desc, props, required):
    schema = {k: {"type": t, "description": d} for k, (t, d) in props.items()}
    return {"name": name, "description": desc,
            "input_schema": {"type": "object", "properties": schema, "required": required}}


PATTERN = ("string", "Optional glob filter such as '*.py' or 'src/**/*.py'")
TOOLS = [
    _tool("list_files", "List project files, optionally filtered by a glob pattern.",
          {"pattern": PATTERN}, []),
    _tool("search", "Search file contents with a Python regex. Returns path:line: text.",
          {"regex": ("string", "Python regular expression"), "pattern": PATTERN}, ["regex"]),
    _tool("read_file", "Read a file with line numbers.",
          {"path": ("string", "File path"), "offset": ("integer", "First line, 1-based"),
           "limit": ("integer", "Max lines (default 2000)")}, ["path"]),
    _tool("write_file", "Create or overwrite a file with the given content.",
          {"path": ("string", "File path"), "content": ("string", "Full file content")},
          ["path", "content"]),
    _tool("edit_file", "Replace exact text in a file. old_text must match exactly once "
          "unless replace_all is true.",
          {"path": ("string", "File path"), "old_text": ("string", "Exact text to replace"),
           "new_text": ("string", "Replacement text"),
           "replace_all": ("boolean", "Replace every match")}, ["path", "old_text", "new_text"]),
    _tool("run", "Run a shell command in the project root and return its output.",
          {"command": ("string", "Shell command"),
           "timeout": ("integer", "Seconds before the command is killed (default 120)")},
          ["command"]),
]
WRITE_TOOLS = {"write_file", "edit_file", "run"}
TOOL_NAMES = {t["name"] for t in TOOLS}


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


class AnthropicProvider:
    """Anthropic Messages API. chat() contract: PROJECT.md section 5."""

    def __init__(self, key, model=DEFAULT_MODEL, base=None):
        self.key, self.model = key, model
        self.base = (base or "https://api.anthropic.com").rstrip("/")

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
        u = reply.get("usage") or {}
        tokens_in = sum(u.get(k, 0) for k in ("input_tokens", "cache_read_input_tokens",
                                              "cache_creation_input_tokens"))
        return text, calls, (tokens_in, u.get("output_tokens", 0))


class OpenAIProvider:
    """OpenAI-compatible Chat Completions (OpenAI, OpenRouter, Ollama, LM Studio, vLLM)."""

    def __init__(self, key, model, base=None):
        self.key, self.model = key, model
        self.base = (base or "https://api.openai.com/v1").rstrip("/")

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
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in tools]
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        reply = post_json(f"{self.base}/chat/completions", headers, body)
        try:
            msg = reply["choices"][0]["message"]
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
        u = reply.get("usage") or {}
        return msg.get("content") or "", calls, (u.get("prompt_tokens", 0), u.get("completion_tokens", 0))


def glob_re(pat):
    """Compile a glob ('*', '?', '**') to a regex. '**/' also matches zero directories."""
    out, i = "", 0
    while i < len(pat):
        if pat.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif pat.startswith("**", i):
            out, i = out + ".*", i + 2
        elif pat[i] in "*?":
            out, i = out + ("[^/]*" if pat[i] == "*" else "[^/]"), i + 1
        else:
            out, i = out + re.escape(pat[i]), i + 1
    return re.compile(out + r"\Z")


def diff(old, new, name):
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        f"a/{name}", f"b/{name}"))


class Tools:
    def __init__(self, root, ui, mode="ask", log=True):
        self.root, self.ui, self.mode, self.log = Path(root).resolve(), ui, mode, log

    def available(self):
        return [t for t in TOOLS if self.mode != "read-only" or t["name"] not in WRITE_TOOLS]

    def call(self, name, args):
        """Run a tool. Never raises: every failure becomes (message, True)."""
        if self.mode == "read-only" and name in WRITE_TOOLS:
            return f"{name} is disabled in read-only mode", True
        fn = getattr(self, "t_" + name, None) if name in TOOL_NAMES else None
        if fn is None:
            return f"unknown tool: {name}", True
        try:
            if not isinstance(args, dict):
                raise TypeError("arguments must be an object")
            if "_invalid_json" in args:
                raise ToolError("bad arguments: not valid JSON: " + str(args["_invalid_json"])[:200])
            return clip(fn(**args)), False
        except ToolError as e:
            return clip(str(e)), True
        except TypeError as e:
            return f"bad arguments: {e}", True
        except Exception as e:
            return f"{type(e).__name__}: {e}", True

    def path(self, p):
        """Resolve p inside the project root. Symlinks are followed, then checked."""
        if not isinstance(p, str) or not p:
            raise ToolError("path must be a non-empty string")
        full = (self.root / p).resolve()
        if full != self.root and self.root not in full.parents:
            raise ToolError(f"path is outside the project root: {p}")
        return full

    def rel(self, p):
        return p.relative_to(self.root).as_posix()

    def walk(self, pattern=None):
        """Yield (path, relpath) for project files, skipping SKIP_DIRS and outside symlinks."""
        rx = glob_re(pattern) if pattern else None
        for dirpath, dirs, files in os.walk(self.root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for name in sorted(files):
                p = Path(dirpath) / name
                rel = self.rel(p)
                if rx and not rx.match(rel if "/" in pattern else name):
                    continue
                if p.is_symlink() and self.root not in p.resolve().parents:
                    continue
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
                if p.stat().st_size > 2_000_000:
                    continue
                data = p.read_bytes()
            except OSError:
                continue
            if b"\0" in data[:4096]:
                continue
            for n, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
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

    def confirm(self, action, target, detail):
        """Ask before a write, edit or command. Raises ToolError if denied."""
        if self.mode == "auto":
            return
        self.ui.emit("approval", action=action, target=target, detail=detail)
        answer = (self.ui.ask("Allow? [y]es / [a]lways / [n]o / or type feedback: ") or "").strip()
        low = answer.lower()
        if low in ("y", "yes"):
            return
        if low in ("a", "always"):
            self.mode = "auto"
            return
        raise ToolError("denied by user" + (f": {answer}" if low not in ("", "n", "no") else ""))

    def audit(self, action, target, **extra):
        """Append a JSON line to .agent/audit.jsonl. Failures never break the agent."""
        if not self.log:
            return
        try:
            folder = self.root / ".agent"
            folder.mkdir(exist_ok=True)
            if not (folder / ".gitignore").exists():
                (folder / ".gitignore").write_text("*\n")
            entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "action": action, "target": target}
            with (folder / "audit.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({**entry, **extra}) + "\n")
        except Exception:
            pass

    def save(self, p, text):
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("w", encoding="utf-8", newline="") as f:
            f.write(text)

    def t_write_file(self, path, content):
        p = self.path(path)
        if p.is_dir():
            raise ToolError(f"path is a directory: {path}")
        rel = self.rel(p)
        old = self.load(p) if p.exists() else None
        if old == content:
            return f"{rel} already has this content"
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
        if count == 0:
            raise ToolError(f"old_text not found in {rel}; re-read the file and copy it exactly")
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
        self.confirm("run", command, command)
        self.audit("run", command)
        posix = os.name == "posix"
        proc = subprocess.Popen(command, shell=True, cwd=self.root, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace", start_new_session=posix)
        expired = []

        def kill():
            try:
                os.killpg(proc.pid, signal.SIGKILL) if posix else proc.kill()
            except OSError:
                pass

        def expire():
            expired.append(True)
            kill()

        timer = threading.Timer(timeout, expire)
        timer.start()
        lines = []
        try:
            for line in proc.stdout:
                lines.append(line)
                self.ui.emit("output", line=line.rstrip("\n"))
            proc.wait()
        except BaseException:
            kill()
            raise
        finally:
            timer.cancel()
            proc.stdout.close()
        status = f"[timed out after {timeout:g}s and was killed]" if expired else f"[exit code {proc.returncode}]"
        return status + "\n" + "".join(lines)
