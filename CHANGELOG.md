# Changelog

All notable changes to kidwit are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). Until 1.0, a minor version may change behaviour.

## [Unreleased]

## [0.1.0] - not yet released

First release.

### Added
- A coding agent in one file of standard-library Python, with Anthropic and OpenAI-compatible providers.
- Six tools: `list_files`, `search`, `read_file`, `write_file`, `edit_file` and `run`.
- Safety: a path jail for the file tools, an approval prompt before every write, edit and command,
  `--read-only`, a guard that always asks for dangerous commands, `/undo`, and an audit log.
- `--json` event protocol for building a UI on top of the engine.
- Context management: old tool output is dropped and, if needed, the older conversation is summarized.
- A small eval suite in `evals/`.
