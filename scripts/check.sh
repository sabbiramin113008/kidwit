#!/usr/bin/env bash
# Local check: Python version, compile, stdlib-only imports, tests and size.
# Safe to re-run. No virtualenv needed: the engine has zero dependencies by design.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
SRC=src/kidwit.py
command -v "$PY" >/dev/null || { echo "✗ $PY not found. Install Python 3.10+."; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "✗ Python 3.10+ required, found $("$PY" --version 2>&1)"; exit 1; }
echo "✓ $("$PY" --version)"

[ -f "$SRC" ] || { echo "✗ $SRC not found"; exit 1; }
"$PY" -m py_compile "$SRC"
echo "✓ $SRC compiles"

# Only standard-library imports allowed.
bad=$("$PY" - <<'PYEOF'
import ast, sys
tree = ast.parse(open("src/kidwit.py").read())
names = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
names |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
print(" ".join(sorted(n for n in names if n not in sys.stdlib_module_names)))
PYEOF
)
[ -z "$bad" ] || { echo "✗ non-stdlib imports: $bad"; exit 1; }
echo "✓ stdlib-only imports"

# The version in pyproject must match the code (it is read from kidwit.VERSION).
if [ -f pyproject.toml ]; then
  ver=$(sed -n 's/^VERSION = __version__ = "\(.*\)"/\1/p' "$SRC")
  [ -n "$ver" ] || { echo "✗ could not read VERSION from $SRC"; exit 1; }
  echo "✓ version $ver"
fi

if [ -d tests ]; then
  "$PY" -m unittest discover -s tests -t . -q
  echo "✓ tests pass"
else
  echo "! tests/ not found, skipping tests"
fi

# Size target is soft: a better agent beats a lower number.
LOC=$(grep -vcE '^\s*(#.*)?$' "$SRC" || true)
TARGET=500
CEILING=700
if [ "$LOC" -gt "$CEILING" ]; then
  echo "✗ size ceiling exceeded: $LOC / $CEILING lines of code"; exit 1
elif [ "$LOC" -gt "$TARGET" ]; then
  echo "! over the ~$TARGET line target: $LOC lines of code (ceiling $CEILING)"
else
  echo "✓ size: $LOC / $TARGET lines of code"
fi
