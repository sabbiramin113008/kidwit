# kidwit

A complete coding-agent engine in one file of standard-library Python, around 500 lines of code. It works inside one directory and asks before it changes anything.

kidwit reads your code, edits files, runs commands and reports back, using native tool calling with Anthropic or any OpenAI-compatible API. It is a from-scratch build and a case study in how little a coding agent needs. It is not a replacement for mature agents.

## Install

You need Python 3.9 or newer. kidwit has no dependencies. From a clone of this repository:

```
pipx install .            # or: pip install .
export ANTHROPIC_API_KEY=sk-ant-...
kidwit "fix the failing test"
```

Or with one line, on macOS and Linux:

```
curl -fsSL https://kidwit.sabbiramin.dev/install | sh
```

The script is `scripts/install.sh` in this repository, so you can read it first. It never uses `sudo` and writes only inside your home directory. It installs with `pipx` or `uv` if you have one. Otherwise it downloads `kidwit.py` from the latest GitHub Release, checks its SHA-256 checksum, and puts a launcher at `~/.local/bin/kidwit`. If there is no release file yet, it installs the PyPI package into its own virtual environment instead. Set `KIDWIT_VERSION=0.1.0rc1` to pick a version, or `KIDWIT_DRY_RUN=1` to see what it would do.

The whole engine is one file, so you can also run it with no install at all:

```
python3 src/kidwit.py "fix the failing test"
```

Without a task, kidwit starts an interactive session and, on a color terminal, shows a small banner. The banner never appears with `--json`, with a task on the command line, or when output is piped. `/clear` resets the conversation, `/verbose` toggles detailed output, and `/exit` (or `/quit`, or end of input) leaves.

## Status

kidwit is early software. This is what has and has not been tested:

1. **Tested live:** the OpenAI-compatible adapter with DeepSeek (`deepseek-chat`) on macOS with Python 3.14. The 8 tasks in `evals/` all pass end to end.
2. **Not yet run against the real service:** the Anthropic adapter. It is covered by the offline tests, but nobody has run it against the live Anthropic API yet.
3. **Tests pass in CI on Linux, macOS and Windows,** with Python 3.14. CI runs the offline tests and the eval self-test, not a live model. Nobody has run kidwit against a live model on Linux or Windows yet. On Windows the `run` tool uses `cmd.exe`, so commands the model writes in Unix shell syntax may fail there.
4. **Untested:** Python 3.9 to 3.13 in CI. The code is written for 3.9 and newer, and 3.10 to 3.13 passed once on Linux and macOS, but they are not part of the regular checks yet.

If you try it somewhere on this list, please open an issue with what happened.

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
AGENT_BASE_URL=http://localhost:11434/v1 AGENT_MODEL=qwen2.5-coder kidwit "add a docstring to main()"
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

1. `ask` (default). Before every `write_file`, `edit_file` and `run`, kidwit shows the diff or command and waits for `y`, `a`, `n`, or any other text. Other text is sent to the model as the reason for the denial. `a` (always) approves that kind of action for the rest of the session: file changes, or commands, but not both.
2. `auto` (`--yes`). No prompts, except for dangerous commands.
3. `read-only` (`--read-only`). The write tools are not offered to the model, and are refused if called anyway.

Some things are handled for you in every mode:

1. In `ask` mode, harmless commands run without a prompt: `ls`, `pwd`, `git status`, `git diff`, `git log`, `git show`, `git branch`, and test runners such as `python3 -m unittest`, `pytest`, `npm test`, `go test` and `cargo test`. They must use plain relative arguments with no shell operators. `cat`, `grep` and `head` are not on the list on purpose: the path jail does not cover `run`, so they could read files outside the project.
2. Dangerous commands always ask, even with `--yes` or after `a`: `rm -rf`, `sudo`, `curl ... | sh`, `git reset --hard`, `git clean`, `git push`, `chmod -R` and similar. With no one to answer (for example `--json` with closed stdin) they are denied.
3. `/undo` in an interactive session reverts the last file write or edit. It does not undo commands.
4. If the model's reply is cut off at the token limit, the half-written tool call is not run. The model is told to do less per call.

Every write, edit and command is appended to `<root>/.agent/audit.jsonl`. kidwit creates `.agent/.gitignore` so the log stays out of git.

## Limits you should know about

The path jail covers the file tools only. `read_file`, `write_file` and `edit_file` refuse paths outside the project root, including `../` and symlinks that lead out, and `list_files` and `search` skip such symlinks. `run` executes real shell commands, and those can reach anything your user account can reach. The protections in v1 are the approval prompt and `--read-only`. There is no OS-level sandbox, so review each command before you approve it, and avoid `--yes` on a machine or directory you care about.

Long sessions are kept within the model's context in two steps. First, old tool output is dropped. If the history is still too large (over about 200,000 characters), kidwit asks the model to summarize the older half of the conversation and continues from that summary.

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
scripts/check.sh                              # everything below in one go
python3 -m unittest discover -s tests -t .    # just the tests
```

`scripts/check.sh` checks the Python version, compiles `src/kidwit.py`, confirms it imports only the standard library, runs the tests, and checks the size (`grep -vcE '^\s*(#.*)?$' src/kidwit.py`). Over 500 lines prints a warning, and over 700 fails.

## Evals

`evals/` holds 8 small tasks, each a tiny repo with a failing check: an off-by-one, a name error, implementing a function, a root cause in a second file, a rename across files, a CRLF file, a missing config default, and a new CLI flag. They show whether a change to kidwit makes it better or worse.

```
python3 evals/run_evals.py --selftest     # checks the tasks themselves, no model needed
python3 evals/run_evals.py                # runs every task against your configured model
python3 evals/run_evals.py --task crlf-file -- --provider openai --model deepseek-chat
```

The runner prints pass or fail, turns, tokens and time per task, and saves the results to `evals/results/`. Be aware that it runs the agent with `--yes`, so the model's commands run as you with no sandbox. Dangerous commands still need approval, and because the runner closes stdin they are denied. Run it only where that is acceptable.

## License

MIT.
