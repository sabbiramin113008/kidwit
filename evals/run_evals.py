#!/usr/bin/env python3
"""Score kidwit on small coding tasks (standard library only).

  python3 evals/run_evals.py --selftest                    check the tasks themselves, no model needed
  python3 evals/run_evals.py                               run every task against the configured model
  python3 evals/run_evals.py --task off-by-one --task crlf-file
  python3 evals/run_evals.py -- --provider openai --model deepseek-chat --base-url URL

Arguments after `--` go to agent.py. The agent runs with --yes inside a throwaway directory,
but its commands are not sandboxed: they run as you. Dangerous commands still need approval,
and stdin is closed, so they are denied. Run this only on a machine where that is acceptable.
Results are printed and saved to evals/results/<timestamp>.json.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from evals.tasks import TASKS  # noqa: E402

AGENT = HERE.parent / "agent.py"


def write_files(root, files):
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content.encode("utf-8"))  # bytes keep CRLF exactly


def verify(root, task):
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}  # a stale .pyc must not hide a fix
    proc = subprocess.run(task["verify"], shell=True, cwd=root, capture_output=True, text=True, timeout=120, env=env)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()[-400:]


def selftest(tasks):
    """Every task must fail as shipped and pass once its reference solution is applied."""
    bad = 0
    for task in tasks:
        root = Path(tempfile.mkdtemp(prefix="kidwit-selftest-"))
        try:
            write_files(root, task["files"])
            before, _ = verify(root, task)
            write_files(root, task["solution"])
            after, detail = verify(root, task)
            ok = (not before) and after
            bad += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {task['id']:<24} fails before: {not before}  passes after: {after}")
            if not ok and detail:
                print("     " + detail.replace("\n", "\n     "))
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return 1 if bad else 0


def run_task(task, agent_args, timeout, keep):
    root = Path(tempfile.mkdtemp(prefix=f"kidwit-eval-{task['id']}-"))
    write_files(root, task["files"])
    cmd = [sys.executable, str(AGENT), "--root", str(root), "--yes", "--no-log", "--json",
           *agent_args, task["prompt"]]
    result = {"id": task["id"], "passed": False, "turns": 0, "input_tokens": 0, "output_tokens": 0,
              "tool_calls": 0, "tool_errors": 0, "error": None, "seconds": 0.0}
    start = time.time()
    try:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event["type"] == "tool":
                result["tool_calls"] += 1
            elif event["type"] == "result" and event["is_error"]:
                result["tool_errors"] += 1
            elif event["type"] == "done":
                result.update(turns=event["turns"], input_tokens=event["input_tokens"],
                              output_tokens=event["output_tokens"])
            elif event["type"] == "error":
                result["error"] = event["message"][:300]
        if proc.returncode != 0 and not result["error"]:
            result["error"] = (proc.stderr or "agent exited with an error")[-300:]
    except subprocess.TimeoutExpired:
        result["error"] = f"timed out after {timeout}s"
    result["seconds"] = round(time.time() - start, 1)
    result["passed"] = verify(root, task)[0]
    if keep:
        result["kept"] = str(root)
    else:
        shutil.rmtree(root, ignore_errors=True)
    return result


def main(argv=None):
    ap = argparse.ArgumentParser(description="Run kidwit on small coding tasks.")
    ap.add_argument("--task", action="append", help="task id to run (repeatable); default: all")
    ap.add_argument("--selftest", action="store_true", help="check the tasks themselves; no model is called")
    ap.add_argument("--timeout", type=int, default=600, help="seconds allowed per task")
    ap.add_argument("--keep", action="store_true", help="keep the working directories")
    ap.add_argument("--out", help="results file (default: evals/results/<timestamp>.json)")
    args, agent_args = ap.parse_known_args(argv)
    agent_args = [a for a in agent_args if a != "--"]
    tasks = [t for t in TASKS if not args.task or t["id"] in args.task]
    unknown = set(args.task or []) - {t["id"] for t in TASKS}
    if unknown:
        print("unknown task: " + ", ".join(sorted(unknown)), file=sys.stderr)
        return 2
    if args.selftest:
        return selftest(tasks)
    results = []
    for task in tasks:
        r = run_task(task, agent_args, args.timeout, args.keep)
        results.append(r)
        note = f"  {r['error']}" if r["error"] else ""
        print(f"{'PASS' if r['passed'] else 'FAIL'} {r['id']:<24} {r['turns']:>2} turns  "
              f"{r['input_tokens']:>7} in {r['output_tokens']:>6} out  {r['seconds']:>6.1f}s{note}", flush=True)
    passed = sum(r["passed"] for r in results)
    total = {k: sum(r[k] for r in results) for k in ("turns", "input_tokens", "output_tokens", "tool_errors")}
    print(f"\n{passed}/{len(results)} passed | {total['turns']} turns | {total['input_tokens']} in / "
          f"{total['output_tokens']} out tokens | {total['tool_errors']} tool errors")
    out = Path(args.out) if args.out else HERE / "results" / (time.strftime("%Y%m%d-%H%M%S") + ".json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"agent_args": agent_args, "passed": passed, "total": len(results),
                               "results": results}, indent=2) + "\n")
    print(f"saved {out}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
