# Security

## Reporting a problem

Please report security problems privately, not in a public issue. Use GitHub's "Report a vulnerability" button on the Security tab of this repository, or email sabbiramin.cse11ruet@gmail.com. Include what you did, what you expected and what happened. I will reply within a few days.

## What kidwit protects, and what it does not

1. The path jail covers the file tools (`read_file`, `write_file`, `edit_file`, `list_files`, `search`). They refuse paths outside the project root, including `../` and symlinks that lead out.
2. The `run` tool executes real shell commands as your user. kidwit asks before each one, and always asks for dangerous ones, but there is no OS-level sandbox. A command you approve can reach anything your account can reach.
3. kidwit sends your task, file contents and command output to the model provider you configure. Do not point it at a provider you do not trust with your code.
4. API keys are read from environment variables. The audit log (`.agent/audit.jsonl`) records the path or the command of each action, so a secret typed into a command line would appear there. kidwit keeps `.agent/` out of git, and `--no-log` turns the log off.

A bypass of the path jail, of the approval prompt or of the danger guard is in scope. A model that makes a bad suggestion that you then approve is not.

## Supported versions

Only the latest release gets fixes while kidwit is below 1.0.
