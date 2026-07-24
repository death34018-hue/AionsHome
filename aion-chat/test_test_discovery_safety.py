import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _is_module_level_asyncio_run(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    function = node.value.func
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "run"
        and isinstance(function.value, ast.Name)
        and function.value.id == "asyncio"
    )


class TestDiscoverySafetyTests(unittest.TestCase):
    def test_test_modules_do_not_start_event_loops_during_import(self):
        offenders = []
        for path in sorted(ROOT.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders.extend(
                f"{path.name}:{node.lineno}"
                for node in tree.body
                if _is_module_level_asyncio_run(node)
            )

        self.assertEqual(
            offenders,
            [],
            "unittest discovery imports test modules; module-level asyncio.run() "
            "can execute real application side effects during collection",
        )


if __name__ == "__main__":
    unittest.main()
