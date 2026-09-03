import ast
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).parent
TEMPLATES = (".env.example", ".env.robinhood", ".env.base", ".env.mainnet")


def configured_environment_variables():
    tree = ast.parse((ROOT / "config.py").read_text())
    names = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        function = node.func
        is_getenv = (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "os"
            and function.attr == "getenv"
        )
        is_gas_cap = isinstance(function, ast.Name) and function.id == "_gas_cap_env"
        first = node.args[0]
        if (is_getenv or is_gas_cap) and isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


class EnvironmentTemplateTests(unittest.TestCase):
    def test_every_template_has_every_config_variable_once(self):
        required = configured_environment_variables()
        for filename in TEMPLATES:
            with self.subTest(template=filename):
                text = (ROOT / filename).read_text()
                assignments = re.findall(r"^([A-Z][A-Z0-9_]*)=", text, re.MULTILINE)
                self.assertEqual(len(assignments), len(set(assignments)), "duplicate active variable")
                self.assertEqual(required - set(assignments), set(), "missing config variable")


if __name__ == "__main__":
    unittest.main()
