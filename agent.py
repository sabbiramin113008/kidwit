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
