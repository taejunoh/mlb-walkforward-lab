from pathlib import Path
import json
import tempfile
import unittest

from mlb_backtest.diagnostics import sha256_file
from mlb_backtest.notebook_artifact import (
    execute_notebook_cells,
    write_executed_notebook,
)


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "analysis/v5_1_bt/diagnostic_100.ipynb"


class NotebookArtifactTest(unittest.TestCase):
    def test_notebook_has_executed_cells_no_errors_and_matching_digest(self):
        notebook = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        self.assertEqual(notebook["nbformat"], 4)
        self.assertEqual(notebook["nbformat_minor"], 5)
        code = [cell for cell in notebook["cells"] if cell["cell_type"] == "code"]
        self.assertTrue(code)
        self.assertEqual(
            [cell["execution_count"] for cell in code],
            list(range(1, len(code) + 1)),
        )
        self.assertFalse(
            any(
                output.get("output_type") == "error"
                for cell in code
                for output in cell["outputs"]
            )
        )
        receipt = json.loads(code[-1]["outputs"][0]["text"][0])
        self.assertEqual(
            receipt["diagnostic_sha256"],
            sha256_file(ROOT / "reports/v5_1_bt/diagnostic_100.json"),
        )
        self.assertEqual(receipt["primary"], {"W": 67, "L": 33, "N": 100})
        self.assertTrue(receipt["recomputed_from_sealed_inputs"])

    def test_regeneration_is_byte_for_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "diagnostic.ipynb"
            write_executed_notebook(ROOT, output)
            first = output.read_bytes()
            write_executed_notebook(ROOT, output)
            self.assertEqual(output.read_bytes(), first)
            self.assertEqual(first, NOTEBOOK.read_bytes())

    def test_executor_captures_stdout_and_propagates_cell_errors(self):
        cells = execute_notebook_cells(("print('captured')\n",), {})
        self.assertEqual(cells[0]["outputs"][0]["text"], ["captured\n"])
        with self.assertRaisesRegex(RuntimeError, "cell failed"):
            execute_notebook_cells(("raise RuntimeError('cell failed')\n",), {})

    def test_failed_execution_preserves_prior_notebook(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "diagnostic.ipynb"
            output.write_bytes(b"prior notebook")
            missing_root = Path(directory) / "missing-root"
            with self.assertRaises(FileNotFoundError):
                write_executed_notebook(missing_root, output)
            self.assertEqual(output.read_bytes(), b"prior notebook")


if __name__ == "__main__":
    unittest.main()
