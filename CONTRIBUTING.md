# Contributing

kidwit is a small, deliberate project. A good change makes the agent better without making it much bigger.

## Ground rules

1. **Standard library only.** `src/kidwit.py` must not import anything outside the Python standard library. `scripts/check.sh` enforces this.
2. **One file.** The engine stays in `src/kidwit.py`.
3. **Size.** The target is about 500 lines of code and the ceiling is 700 (`scripts/check.sh` counts them). It is a soft target: a better agent matters more than hitting the number. A change that adds lines should say why they are worth it.
4. **Tests.** Every change comes with a test in `tests/test_kidwit.py`. The tests use only `unittest` and need no API key.
5. **Python 3.10 or newer.** No syntax or library features from later versions.

## Before you open a pull request

```
scripts/check.sh                       # compiles, checks imports, runs the tests, checks the size
python3 evals/run_evals.py --selftest  # checks the eval tasks themselves
```

If your change affects how the agent behaves with a model, also run the evals against a model and include the pass count (`python3 evals/run_evals.py`). Note that the evals run the agent with `--yes`, so read the warning in `evals/run_evals.py` first.

## Commits

Use a short imperative summary, for example `Fix edit_file match on CRLF files`. One logical change per commit.

## Reporting bugs

Open an issue with the command you ran, the provider and model, your Python version and operating system, and the output. Windows and Linux reports are especially welcome, since those are not tested live yet. For security problems, see `SECURITY.md`.
