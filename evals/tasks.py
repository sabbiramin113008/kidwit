"""Eval tasks for kidwit. Each task is a tiny repo with a failing check.

A task has: id, prompt, files (the starting repo), verify (a shell command that must exit 0
when the task is solved) and solution (files that replace the starting ones, used only to
check that the task itself is sound: `python3 evals/run_evals.py --selftest`).
"""

UNITTEST = "python3 -m unittest -q"

TASKS = [
    {
        "id": "off-by-one",
        "prompt": "The tests fail. Find the bug and fix it.",
        "files": {
            "calc.py": "def add(a, b):\n    return a - b\n",
            "test_calc.py": ("import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
                             "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n"),
        },
        "verify": f"{UNITTEST} test_calc",
        "solution": {"calc.py": "def add(a, b):\n    return a + b\n"},
    },
    {
        "id": "name-error",
        "prompt": "test_stats crashes. Fix the code, not the test.",
        "files": {
            "stats.py": "def mean(values):\n    total = sum(values)\n    return totl / len(values)\n",
            "test_stats.py": ("import unittest\nfrom stats import mean\n\n\nclass T(unittest.TestCase):\n"
                              "    def test_mean(self):\n        self.assertEqual(mean([2, 4, 6]), 4)\n"),
        },
        "verify": f"{UNITTEST} test_stats",
        "solution": {"stats.py": "def mean(values):\n    total = sum(values)\n    return total / len(values)\n"},
    },
    {
        "id": "implement-slugify",
        "prompt": "Implement slugify() in slug.py so the tests in test_slug.py pass.",
        "files": {
            "slug.py": "def slugify(text):\n    raise NotImplementedError\n",
            "test_slug.py": ("import unittest\nfrom slug import slugify\n\n\nclass T(unittest.TestCase):\n"
                             "    def test_basic(self):\n        self.assertEqual(slugify('Hello World'), 'hello-world')\n"
                             "    def test_punctuation(self):\n"
                             "        self.assertEqual(slugify('  Hello, World!  '), 'hello-world')\n"
                             "    def test_runs(self):\n        self.assertEqual(slugify('a  --  b'), 'a-b')\n"),
        },
        "verify": f"{UNITTEST} test_slug",
        "solution": {"slug.py": ("import re\n\n\ndef slugify(text):\n"
                                 "    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n")},
    },
    {
        "id": "root-cause-two-files",
        "prompt": "test_cart fails. Find the root cause and fix it.",
        "files": {
            "pricing.py": "def apply_discount(price, percent):\n    return price * percent / 100\n",
            "cart.py": ("from pricing import apply_discount\n\n\ndef total(prices, percent=0):\n"
                        "    return sum(apply_discount(p, percent) if percent else p for p in prices)\n"),
            "test_cart.py": ("import unittest\nfrom cart import total\n\n\nclass T(unittest.TestCase):\n"
                             "    def test_plain(self):\n        self.assertEqual(total([10, 20]), 30)\n"
                             "    def test_discount(self):\n        self.assertEqual(total([100, 50], 10), 135)\n"),
        },
        "verify": f"{UNITTEST} test_cart",
        "solution": {"pricing.py": "def apply_discount(price, percent):\n    return price * (100 - percent) / 100\n"},
    },
    {
        "id": "rename-everywhere",
        "prompt": "Rename the function fetch to load, and update every place that uses it.",
        "files": {
            "utils.py": "def fetch(key):\n    return {'a': 1, 'b': 2}[key]\n",
            "one.py": "from utils import fetch\n\n\ndef first():\n    return fetch('a')\n",
            "two.py": "import utils\n\n\ndef second():\n    return utils.fetch('b')\n",
            "test_rename.py": ("import unittest\nimport one\nimport two\nfrom utils import load\n\n\n"
                               "class T(unittest.TestCase):\n    def test_all(self):\n"
                               "        self.assertEqual((one.first(), two.second(), load('a')), (1, 2, 1))\n"),
        },
        "verify": f"{UNITTEST} test_rename && ! grep -rw fetch --include=*.py utils.py one.py two.py",
        "solution": {
            "utils.py": "def load(key):\n    return {'a': 1, 'b': 2}[key]\n",
            "one.py": "from utils import load\n\n\ndef first():\n    return load('a')\n",
            "two.py": "import utils\n\n\ndef second():\n    return utils.load('b')\n",
        },
    },
    {
        "id": "crlf-file",
        "prompt": "test_greet fails. Fix greet.py and keep its Windows line endings.",
        "files": {
            "greet.py": "def greet(name):\r\n    return 'Hello ' + nme\r\n",
            "test_greet.py": ("import unittest\nfrom greet import greet\n\n\nclass T(unittest.TestCase):\n"
                              "    def test_greet(self):\n        self.assertEqual(greet('Ana'), 'Hello Ana')\n"),
        },
        "verify": (f"{UNITTEST} test_greet && python3 -c \"d = open('greet.py', 'rb').read(); "
                   "assert b'\\r\\n' in d and d.count(b'\\n') == d.count(b'\\r\\n')\""),
        "solution": {"greet.py": "def greet(name):\r\n    return 'Hello ' + name\r\n"},
    },
    {
        "id": "default-config",
        "prompt": "load_retries() crashes when config.json has no 'retries'. It should return 3 in that case.",
        "files": {
            "config.json": "{\"timeout\": 30}\n",
            "app.py": ("import json\n\n\ndef load_retries(path='config.json'):\n"
                       "    with open(path) as f:\n        return json.load(f)['retries']\n"),
            "test_app.py": ("import unittest\nfrom app import load_retries\n\n\nclass T(unittest.TestCase):\n"
                            "    def test_default(self):\n        self.assertEqual(load_retries(), 3)\n"),
        },
        "verify": f"{UNITTEST} test_app",
        "solution": {"app.py": ("import json\n\n\ndef load_retries(path='config.json'):\n"
                                "    with open(path) as f:\n        return json.load(f).get('retries', 3)\n")},
    },
    {
        "id": "add-cli-flag",
        "prompt": "Add a --shout flag to greet_cli.py that prints the greeting in upper case. Keep the default unchanged.",
        "files": {
            "greet_cli.py": ("import sys\n\nname = sys.argv[1] if len(sys.argv) > 1 else 'world'\n"
                             "print(f'Hello, {name}!')\n"),
            "test_cli.py": ("import subprocess\nimport sys\nimport unittest\n\n\n"
                            "def run(*args):\n    return subprocess.run([sys.executable, 'greet_cli.py', *args],\n"
                            "                          capture_output=True, text=True).stdout.strip()\n\n\n"
                            "class T(unittest.TestCase):\n    def test_default(self):\n"
                            "        self.assertEqual(run('Ana'), 'Hello, Ana!')\n"
                            "    def test_shout(self):\n        self.assertEqual(run('Ana', '--shout'), 'HELLO, ANA!')\n"
                            "        self.assertEqual(run('--shout', 'Ana'), 'HELLO, ANA!')\n"),
        },
        "verify": f"{UNITTEST} test_cli",
        "solution": {"greet_cli.py": ("import sys\n\nargs = [a for a in sys.argv[1:] if a != '--shout']\n"
                                      "name = args[0] if args else 'world'\n"
                                      "text = f'Hello, {name}!'\n"
                                      "print(text.upper() if '--shout' in sys.argv else text)\n")},
    },
]
