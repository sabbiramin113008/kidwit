# Changelog

All notable changes to kidwit are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change behaviour.

## [Unreleased]

### Fixed
- Windows: a command that timed out left its child processes running. kidwit now ends the whole process tree.
- The eval task `rename-everywhere` no longer needs a Unix shell to check its result.

## [0.1.0] - not yet released

First release.

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
