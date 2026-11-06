#!/usr/bin/env bash
# Local check: Python version, compile, tests, stdlib-only imports and line budget.
# Safe to re-run. No virtualenv needed: the project has zero dependencies by design (PROJECT.md D2).
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "✗ $PY not found. Install Python 3.9+."; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || { echo "✗ Python 3.9+ required, found $("$PY" --version 2>&1)"; exit 1; }
echo "✓ $("$PY" --version)"

[ -f agent.py ] || { echo "✗ agent.py not found"; exit 1; }
"$PY" -m py_compile agent.py
echo "✓ agent.py compiles"

# Only standard-library imports allowed (PROJECT.md D2).
bad=$("$PY" - <<'EOF'
import ast, sys
if not hasattr(sys, "stdlib_module_names"):  # Python 3.9 lacks it; check on 3.10+
    sys.exit(0)
tree = ast.parse(open("agent.py").read())
names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
print(" ".join(sorted(n for n in names if n not in sys.stdlib_module_names)))
EOF
)
[ -z "$bad" ] || { echo "✗ non-stdlib imports: $bad"; exit 1; }
echo "✓ stdlib-only imports"

if [ -f test_agent.py ]; then
  "$PY" -m unittest -q test_agent.py
  echo "✓ tests pass"
else
  echo "! test_agent.py not found yet, skipping tests"
fi

LOC=$(grep -vcE '^\s*(#.*)?$' agent.py || true)
LIMIT=500
if [ "$LOC" -gt "$LIMIT" ]; then
  echo "✗ line budget exceeded: $LOC / $LIMIT lines of code (PROJECT.md D4)"; exit 1
fi
echo "✓ line budget: $LOC / $LIMIT lines of code"