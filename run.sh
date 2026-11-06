#!/usr/bin/env bash
# Try kidwit against DeepSeek using the values in .env.
#
#   ./run.sh                       fix a failing test in a fresh throwaway demo repo
#   ./run.sh -i                    interactive session in the current directory
#   ./run.sh -i --demo             interactive session in a fresh throwaway demo repo
#   ./run.sh "your task"           run one task in the current directory
#   ./run.sh --root DIR "task"     any kidwit flags are passed straight through
#   ./run.sh --evals [--task ID]   score kidwit on the eval tasks (runs the agent with --yes; see README)
#   ./run.sh --evals --selftest    check the eval tasks themselves, no model needed
#
# Inside an interactive session: /clear resets the conversation, /exit (or Ctrl-D) quits.
# Approval mode is the default: you answer y / a / n for every write, edit and command.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

[ -f "$HERE/.env" ] || { echo "✗ $HERE/.env not found (needs UPSTREAM_BASE_URL and UPSTREAM_API_KEY)"; exit 1; }
set -a; . "$HERE/.env"; set +a
: "${UPSTREAM_API_KEY:?UPSTREAM_API_KEY is missing in .env}"
: "${UPSTREAM_BASE_URL:?UPSTREAM_BASE_URL is missing in .env}"

# kidwit reads these names; .env uses UPSTREAM_*.
export OPENAI_API_KEY="$UPSTREAM_API_KEY"
export AGENT_BASE_URL="$UPSTREAM_BASE_URL"
export AGENT_PROVIDER=openai
export AGENT_MODEL="${AGENT_MODEL:-deepseek-chat}"

# Colors only on a terminal, and never when NO_COLOR is set.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  BOLD=$'\033[1m'; DIM=$'\033[2m'; CYAN=$'\033[36m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
  COLOR=1
else
  BOLD=''; DIM=''; CYAN=''; GREEN=''; RED=''; YELLOW=''; RESET=''
  COLOR=0
fi

banner() {
  COLOR="$COLOR" python3 - <<'PY'
import os

ART = [
    "██╗  ██╗██╗██████╗ ██╗    ██╗██╗████████╗",
    "██║ ██╔╝██║██╔══██╗██║    ██║██║╚══██╔══╝",
    "█████╔╝ ██║██║  ██║██║ █╗ ██║██║   ██║   ",
    "██╔═██╗ ██║██║  ██║██║███╗██║██║   ██║   ",
    "██║  ██╗██║██████╔╝╚███╔███╔╝██║   ██║   ",
    "╚═╝  ╚═╝╚═╝╚═════╝  ╚══╝╚══╝ ╚═╝   ╚═╝   ",
]
RULE = "━" * len(ART[0])
LINES = [
    ("▸ a coding agent in one file of standard-library Python", False),
    ("  read · search · edit · run   and it always asks first", True),
]
color = os.environ.get("COLOR") == "1"
truecolor = os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
RESET = "\033[0m" if color else ""
STOPS = [(255, 95, 158), (150, 110, 255), (0, 212, 255)]  # pink -> violet -> cyan


def shade(t, brightness=1.0):
    """Color at position t (0..1) along the gradient, as an escape code."""
    t = min(max(t, 0.0), 1.0) * (len(STOPS) - 1)
    i = min(int(t), len(STOPS) - 2)
    r, g, b = (round((STOPS[i][k] + (STOPS[i + 1][k] - STOPS[i][k]) * (t - i)) * brightness) for k in range(3))
    if truecolor:
        return f"\033[38;2;{r};{g};{b}m"
    cube = lambda v: round(v / 255 * 5)
    return f"\033[38;5;{16 + 36 * cube(r) + 6 * cube(g) + cube(b)}m"


def gradient(text, y=0, rows=1, brightness=1.0):
    if not color:
        return text
    out = ""
    for x, ch in enumerate(text):
        out += ch if ch == " " else shade(x / len(text) * 0.85 + y / rows * 0.15, brightness) + ch
    return out + RESET


print()
for y, row in enumerate(ART):
    line = ""
    for x, ch in enumerate(row):
        # solid blocks get the full gradient; the box-drawing "shadow" strokes are darker
        if color and ch != " ":
            line += shade(x / len(row) * 0.85 + y / len(ART) * 0.15, 1.0 if ch == "█" else 0.55)
        line += ch
    print(line + RESET)
print(gradient(RULE))
for text, _ in LINES:
    print(gradient(text, brightness=0.9))
print()
PY
}

INTERACTIVE=0
DEMO=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    -i|--interactive) INTERACTIVE=1; shift ;;
    --demo) DEMO=1; shift ;;
    --evals) shift; exec python3 "$HERE/evals/run_evals.py" "$@" ;;
    *) break ;;
  esac
done

make_demo() {
  DEMO_DIR="$(mktemp -d "${TMPDIR:-/tmp}/kidwit-demo.XXXXXX")"
  cat > "$DEMO_DIR/calc.py" <<'EOF'
def add(a, b):
    return a - b
EOF
  cat > "$DEMO_DIR/test_calc.py" <<'EOF'
import unittest
from calc import add


class AddTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)


unittest.main()
EOF
  echo "${BOLD}demo repo${RESET}  $DEMO_DIR ${DIM}(has a failing test in calc.py)${RESET}"
}

if [ "$INTERACTIVE" -eq 1 ]; then
  # kidwit prints its own small banner in interactive sessions.
  echo "${BOLD}model${RESET}      ${CYAN}$AGENT_MODEL${RESET} ${DIM}via $AGENT_BASE_URL${RESET}"
  echo "${BOLD}approvals${RESET}  answer ${GREEN}y${RESET} / ${YELLOW}a${RESET} (always) / n, or type feedback. ${DIM}/clear resets, /exit quits.${RESET}"
  if [ "$DEMO" -eq 1 ]; then
    make_demo
    exec python3 "$HERE/src/kidwit.py" --root "$DEMO_DIR" "$@"
  fi
  exec python3 "$HERE/src/kidwit.py" "$@"
fi

if [ "$#" -gt 0 ]; then
  exec python3 "$HERE/src/kidwit.py" "$@"
fi

banner
make_demo
echo "${BOLD}model${RESET}      ${CYAN}$AGENT_MODEL${RESET} ${DIM}via $AGENT_BASE_URL${RESET}"
echo "${BOLD}approvals${RESET}  answer each prompt with ${GREEN}y${RESET} / ${YELLOW}a${RESET} (always) / n"
echo
set +e
python3 "$HERE/src/kidwit.py" --root "$DEMO_DIR" "fix the failing test"
status=$?
set -e

echo
if [ "$status" -eq 0 ]; then
  echo "${BOLD}agent exit code${RESET}  ${GREEN}$status${RESET}"
else
  echo "${BOLD}agent exit code${RESET}  ${RED}$status${RESET}"
fi
echo "${BOLD}running the test again${RESET}"
if (cd "$DEMO_DIR" && python3 test_calc.py); then
  echo "${GREEN}${BOLD}test passes${RESET}"
else
  echo "${YELLOW}${BOLD}test still fails${RESET}"
fi
echo "${DIM}audit log: $DEMO_DIR/.agent/audit.jsonl${RESET}"
exit "$status"
