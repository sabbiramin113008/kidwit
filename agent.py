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
