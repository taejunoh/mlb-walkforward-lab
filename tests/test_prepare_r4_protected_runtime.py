"""The former Task 2a executable entry point must remain retired."""

from __future__ import annotations

import ast
import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]


class PrepareRetirementTests(unittest.TestCase):
    def test_prepare_is_an_unconditional_no_execution_deprecation(self) -> None:
        path = ROOT / "scripts" / "prepare_r4_protected_runtime.py"
        tree = ast.parse(path.read_text())
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        self.assertFalse(
            imports & {"subprocess", "socket", "urllib", "http", "tarfile"}
        )
        spec = importlib.util.spec_from_file_location("retired_prepare", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with self.assertRaisesRegex(RuntimeError, "R4_TASK2A_DEPRECATED_NO_EXECUTION"):
            module.main()


if __name__ == "__main__":
    unittest.main()
