from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from mlb_backtest.diagnostics import sha256_file
from mlb_backtest.notebook_artifact import (
    execute_notebook_cells,
    notebook_sources,
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

    def test_executor_captures_stderr_separately_including_warnings(self):
        cells = execute_notebook_cells(
            (
                "import sys, warnings\n"
                "print('normal')\n"
                "print('problem', file=sys.stderr)\n"
                "warnings.warn('warning text', UserWarning)\n",
            ),
            {},
        )
        outputs = cells[0]["outputs"]
        self.assertEqual(outputs[0], {
            "name": "stdout",
            "output_type": "stream",
            "text": ["normal\n"],
        })
        self.assertEqual(outputs[1]["name"], "stderr")
        self.assertIn("problem\n", outputs[1]["text"])
        self.assertTrue(any("warning text" in line for line in outputs[1]["text"]))

    def test_notebook_bytes_are_clone_portable_and_guard_import_origin(self):
        sources = notebook_sources(ROOT)
        alternate = Path("/tmp/another-clone/mlb-v5-1-backtest")
        self.assertEqual(sources, notebook_sources(alternate))
        source_text = "".join(sources)
        self.assertNotIn(str(ROOT), source_text)
        self.assertIn("diagnostics.__file__", source_text)
        self.assertIn("does not come from repository root", source_text)
        self.assertNotIn(str(ROOT), NOTEBOOK.read_text(encoding="utf-8"))

    def test_failed_execution_preserves_prior_notebook(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "diagnostic.ipynb"
            output.write_bytes(b"prior notebook")
            missing_root = Path(directory) / "missing-root"
            with self.assertRaisesRegex(ValueError, "does not come from repository root"):
                write_executed_notebook(missing_root, output)
            self.assertEqual(output.read_bytes(), b"prior notebook")

    def test_optimized_execution_rejects_tampered_canonical_json(self):
        with tempfile.TemporaryDirectory() as directory:
            clone = Path(directory) / "portable-clone"
            shutil.copytree(ROOT / "mlb_backtest", clone / "mlb_backtest")
            shutil.copytree(ROOT / "config", clone / "config")
            os.symlink(ROOT / "data", clone / "data", target_is_directory=True)
            os.symlink(ROOT / "audit", clone / "audit", target_is_directory=True)
            report_dir = clone / "reports/v5_1_bt"
            report_dir.mkdir(parents=True)
            canonical = (ROOT / "reports/v5_1_bt/diagnostic_100.json").read_bytes()
            (report_dir / "diagnostic_100.json").write_bytes(canonical + b" ")
            output = clone / "analysis/v5_1_bt/diagnostic_100.ipynb"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"prior notebook")
            command = (
                "from pathlib import Path; "
                "from mlb_backtest.notebook_artifact import write_executed_notebook; "
                "write_executed_notebook(Path.cwd(), "
                "Path('analysis/v5_1_bt/diagnostic_100.ipynb'))"
            )
            completed = subprocess.run(
                [sys.executable, "-O", "-c", command],
                cwd=clone,
                capture_output=True,
                check=False,
                text=True,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("canonical diagnostic differs", completed.stderr)
            self.assertEqual(output.read_bytes(), b"prior notebook")


if __name__ == "__main__":
    unittest.main()
