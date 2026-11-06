import unittest

import agent


class SmokeTest(unittest.TestCase):
    def test_imports(self):
        self.assertTrue(hasattr(agent, "__doc__"))


if __name__ == "__main__":
    unittest.main()
