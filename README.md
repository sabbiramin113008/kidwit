# kidwit

A complete coding-agent engine in one file of standard-library Python, around 500 lines of code. It works inside one directory and asks before it changes anything.

kidwit reads your code, edits files, runs commands and reports back, using native tool calling with Anthropic or any OpenAI-compatible API. It is a from-scratch build and a case study in how little a coding agent needs. It is not a replacement for mature agents.

## Install

You need Python 3.9 or newer. There is nothing else to install. Download `agent.py`, for example with `curl -O <raw URL of agent.py>`, and run it:

```
export ANTHROPIC_API_KEY=sk-ant-...
python3 agent.py "fix the failing test"
```

Without a task, kidwit starts an interactive session and, on a color terminal, shows a small banner. The banner never appears with `--json`, with a task on the command line, or when output is piped. `/clear` resets the conversation, `/verbose` toggles detailed output, and `/exit` (or `/quit`, or end of input) leaves.

## Configuration

Environment variables:

| Variable | Meaning |
|---|---|
| `ANTHROPIC_API_KEY` | Key for the Anthropic Messages API. If set, Anthropic is the default provider. |
| `OPENAI_API_KEY` | Key for an OpenAI-compatible API. Optional for local servers. |
| `AGENT_PROVIDER` | `anthropic` or `openai`. |
| `AGENT_MODEL` | Model name. Defaults to `claude-sonnet-5-5` for Anthropic. Required for `openai`. |
| `AGENT_BASE_URL` | API base URL, for OpenRouter, Ollama, LM Studio, vLLM or a proxy. |

Provider choice follows this order: `--provider`, then `AGENT_PROVIDER`, then Anthropic if `ANTHROPIC_API_KEY` is set, then OpenAI-compatible.

Local model example:

```
AGENT_BASE_URL=http://localhost:11434/v1 AGENT_MODEL=qwen2.5-coder python3 agent.py "add a docstring to main()"
```

Flags:

| Flag | Meaning |
|---|---|
| `--root DIR` | Project directory. Default: the current directory. |
| `--provider anthropic\|openai` | Choose the API. |
| `--model M`, `--base-url URL` | Override `AGENT_MODEL` and `AGENT_BASE_URL`. |
| `--yes` | Never ask for approval. |
| `--read-only` | Offer only the read tools. Writes and commands are not available. |
| `--json` | Print JSON-lines events instead of text. |
| `--verbose` | Show live command output and full tool arguments. By default kidwit prints one short dim line per tool call and then the answer. |
| `--no-log` | Do not write `.agent/audit.jsonl`. |
| `--version` | Print the version. |

A task given on the command line runs once. The exit code is 0 on success and 1 on error, so it works in scripts and benchmarks.

## Reading the terminal

On a color terminal each kind of text looks different. Your prompt is a bold green `>>`. The model's replies have a magenta `▌` gutter. Tool calls are one dim line with the tool name in cyan. Permission requests start with a yellow `┌ permission needed` line, followed by the diff or command and a yellow `└ allow?` prompt. Errors are red. With `NO_COLOR`, piped output or `--json`, none of this styling is printed.

## Tools

The model gets six tools: `list_files`, `search`, `read_file`, `write_file`, `edit_file` and `run`. Inside a git repository, `list_files` and `search` honour `.gitignore`, so build output and similar files stay out of the way. Edits are exact-string replacement: a match that differs only in trailing whitespace is accepted, and when nothing matches the error names the closest line so the model can correct itself. `old_text` must match exactly once unless `replace_all` is set, and files with Windows line endings keep them.

If the project root has an `AGENTS.md` (or else a `CLAUDE.md`), its first 20,000 characters are added to the system prompt.

## Approval modes

1. `ask` (default). Before every `write_file`, `edit_file` and `run`, kidwit shows the diff or command and waits for `y`, `a` (always, which switches to auto for the rest of the session), `n`, or any other text. Other text is sent to the model as the reason for the denial.
2. `auto` (`--yes`). No prompts.
3. `read-only` (`--read-only`). The write tools are not offered to the model, and are refused if called anyway.

Every write, edit and command is appended to `<root>/.agent/audit.jsonl`. kidwit creates `.agent/.gitignore` so the log stays out of git.

## Limits you should know about

The path jail covers the file tools only. `read_file`, `write_file` and `edit_file` refuse paths outside the project root, including `../` and symlinks that lead out, and `list_files` and `search` skip such symlinks. `run` executes real shell commands, and those can reach anything your user account can reach. The protections in v1 are the approval prompt and `--read-only`. There is no OS-level sandbox, so review each command before you approve it, and avoid `--yes` on a machine or directory you care about.

Other deliberate omissions: no streaming of model responses (command output does stream), no sub-agents, no MCP, no saved sessions, and no UI beyond plain terminal text.

## Event protocol

With `--json`, kidwit writes one JSON object per line on stdout. Tasks and approval answers are read from stdin, one line each. End of input during an approval counts as "no". A UI can drive the engine through this protocol.

| `type` | Fields |
|---|---|
| `info` | `message` |
| `text` | `text` |
| `tool` | `id`, `name`, `input` |
| `output` | `line` (live command output) |
| `approval` | `action` (`write`, `edit` or `run`), `target`, `detail` (diff or command) |
| `result` | `id`, `output`, `is_error` |
| `done` | `turns`, `input_tokens`, `output_tokens` |
| `error` | `message` |

## Tests

The tests use only `unittest` and need no API key. A fake local HTTP server stands in for both APIs.

```
scripts/check.sh
```

This checks the Python version, compiles `agent.py`, confirms it imports only the standard library, runs the tests, and checks the size (`grep -vcE '^\s*(#.*)?$' agent.py`). Over 500 lines prints a warning, and over 700 fails.

## License

MIT.
