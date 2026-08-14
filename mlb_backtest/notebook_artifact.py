from collections.abc import Sequence
from pathlib import Path
import contextlib
import io

from mlb_backtest.diagnostics import canonical_json_bytes


def execute_notebook_cells(
    sources: Sequence[str], globals_dict: dict[str, object]
) -> list[dict[str, object]]:
    cells: list[dict[str, object]] = []
    for count, source in enumerate(sources, 1):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            exec(
                compile(source, f"diagnostic_100_cell_{count}", "exec"),
                globals_dict,
            )
        outputs = []
        for name, stream in (("stdout", stdout), ("stderr", stderr)):
            text = stream.getvalue()
            if text:
                outputs.append(
                    {
                        "name": name,
                        "output_type": "stream",
                        "text": text.splitlines(keepends=True),
                    }
                )
        cells.append(
            {
                "cell_type": "code",
                "execution_count": count,
                "metadata": {},
                "source": source.splitlines(keepends=True),
                "outputs": outputs,
            }
        )
    return cells


def notebook_sources(root: Path) -> tuple[str, ...]:
    return (
        "from pathlib import Path\n"
        "import importlib\n"
        "import json\n"
        "import sys\n"
        "ROOT = globals().get('NOTEBOOK_REPOSITORY_ROOT')\n"
        "if ROOT is None:\n"
        "    markers = ('audit/v5_1_bt/lock_manifest.json', 'reports/v5_1_bt/diagnostic_100.json', 'mlb_backtest')\n"
        "    ROOT = next((candidate for candidate in (Path.cwd(), *Path.cwd().parents) if all((candidate / marker).exists() for marker in markers)), None)\n"
        "if ROOT is None:\n"
        "    raise ValueError('repository root cannot be discovered')\n"
        "ROOT = Path(ROOT).resolve()\n"
        "root_text = str(ROOT)\n"
        "sys.path[:] = [root_text, *(entry for entry in sys.path if entry != root_text)]\n"
        "diagnostics = importlib.import_module('mlb_backtest.diagnostics')\n"
        "diagnostics_origin = Path(diagnostics.__file__).resolve()\n"
        "if (ROOT / 'mlb_backtest').resolve() not in diagnostics_origin.parents:\n"
        "    raise ValueError('mlb_backtest.diagnostics does not come from repository root')\n"
        "recomputed = diagnostics.build_diagnostic(diagnostics.load_v51_evidence(ROOT))\n"
        "saved_path = ROOT / 'reports/v5_1_bt/diagnostic_100.json'\n"
        "saved_bytes = saved_path.read_bytes()\n"
        "if diagnostics.canonical_json_bytes(recomputed) + b'\\n' != saved_bytes:\n"
        "    raise ValueError('canonical diagnostic differs from recomputed sealed inputs')\n"
        "saved = json.loads(saved_bytes)\n",
        "print(json.dumps({'primary': saved['primary'], 'baselines': saved['baselines']}, ensure_ascii=False, sort_keys=True))\n",
        "print(json.dumps({'calibration': saved['calibration'], 'rank': saved['rank'], 'monthly': saved['monthly']}, ensure_ascii=False, sort_keys=True))\n",
        "print(json.dumps({'components': saved['components'], 'data_quality': saved['data_quality'], 'leakage': saved['leakage']}, ensure_ascii=False, sort_keys=True))\n",
        "print(json.dumps({'diagnostic_sha256': diagnostics.sha256_file(saved_path), 'primary': saved['primary']['record'], 'recomputed_from_sealed_inputs': True}, sort_keys=True))\n",
    )


def write_executed_notebook(root: Path, output: Path) -> None:
    cells = execute_notebook_cells(
        notebook_sources(root),
        {
            "__name__": "__diagnostic_notebook__",
            "NOTEBOOK_REPOSITORY_ROOT": root.resolve(),
        },
    )
    notebook = {
        "cells": cells,
        "metadata": {
            "kernelspec": {
                "display_name": "Python 3",
                "language": "python",
                "name": "python3",
            },
            "language_info": {"name": "python", "version": "3"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".ipynb.tmp")
    temporary.write_bytes(canonical_json_bytes(notebook) + b"\n")
    temporary.replace(output)
