## What and why

## Checklist

1. `scripts/check.sh` passes (tests, standard-library-only imports, size ceiling).
2. A test covers the change.
3. No new dependencies. `src/kidwit.py` stays one file.
4. If the change affects behaviour with a model, I ran `python3 evals/run_evals.py` and noted the result.
