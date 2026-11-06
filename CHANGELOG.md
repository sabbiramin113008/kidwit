# Changelog

All notable changes to kidwit are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change behaviour.

## [Unreleased]

### Added
- The interactive prompt now supports arrow-key editing and history (Up and Down) on macOS and Linux. The green `>>` prompt keeps its color: what you type is green too, and kidwit resets the color when you press Enter. Windows is unchanged.

## [0.1.0] - 2026-11-07

The first stable release. It is the same code as 0.1.0rc2, with the checks widened to every supported Python version.

### Known limitations
- Tested live only with the OpenAI-compatible adapter (DeepSeek), on macOS with Python 3.14.
- The Anthropic adapter is covered by offline tests but has not been run against the live API.
- No live-model runs yet on Linux or Windows. CI runs the offline tests on Linux (Python 3.10 to 3.14) and on macOS and Windows (3.10 and 3.14).
- On Windows the `run` tool uses `cmd.exe`, so commands written in Unix shell syntax may fail.

## [0.1.0rc2] - 2026-11-07

The second release candidate.

### Changed
- Python 3.10 or newer is required (was 3.9). Only 3.10 to 3.14 have been run.

### Added
- `scripts/install.sh`, a one-line installer: `curl -fsSL https://kidwit.sabbiramin.dev/install | sh`. It uses pipx or uv, or downloads `kidwit.py` and checks its SHA-256 checksum, or installs from PyPI into a private virtual environment.

### Fixed
- Windows: a command that timed out left its child processes running. kidwit now ends the whole process tree.
- The eval task `rename-everywhere` no longer needs a Unix shell to check its result.

## [0.1.0rc1] - 2026-11-06

First release candidate.

### Known limitations
- Tested live only with the OpenAI-compatible adapter (DeepSeek) on macOS with Python 3.14.
- The Anthropic adapter is covered by offline tests but has not been run against the live API.
- CI runs the offline tests on Linux, macOS and Windows with Python 3.14 only. No live-model runs yet on Linux or Windows, and Python 3.9 to 3.13 are not in CI.

### Added
- A coding agent in one file of standard-library Python, with Anthropic and OpenAI-compatible providers.
- Six tools: `list_files`, `search`, `read_file`, `write_file`, `edit_file` and `run`.
- Safety: a path jail for the file tools, an approval prompt before every write, edit and command,
  `--read-only`, a guard that always asks for dangerous commands, `/undo`, and an audit log.
- `--json` event protocol for building a UI on top of the engine.
- Context management: old tool output is dropped and, if needed, the older conversation is summarized.
- A small eval suite in `evals/`.
