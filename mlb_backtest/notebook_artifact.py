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
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            exec(
                compile(source, f"diagnostic_100_cell_{count}", "exec"),
                globals_dict,
            )
        cells.append(
            {
                "cell_type": "code",
                "execution_count": count,
                "metadata": {},
                "source": source.splitlines(keepends=True),
                "outputs": [
                    {
                        "name": "stdout",
                        "output_type": "stream",
                        "text": stream.getvalue().splitlines(keepends=True),
                    }
                ],
            }
        )
    return cells


def notebook_sources(root: Path) -> tuple[str, ...]:
    root_literal = repr(str(root.resolve()))
    return (
        "from pathlib import Path\n"
        "from mlb_backtest.diagnostics import load_v51_evidence, build_diagnostic, canonical_json_bytes, sha256_file\n"
        "import json\n"
        "ROOT = Path(" + root_literal + ")\n"
        "recomputed = build_diagnostic(load_v51_evidence(ROOT))\n"
        "saved_path = ROOT / 'reports/v5_1_bt/diagnostic_100.json'\n"
        "saved = json.loads(saved_path.read_text())\n"
        "assert canonical_json_bytes(recomputed) + b'\\n' == saved_path.read_bytes()\n",
        "print(json.dumps({'primary': saved['primary'], 'baselines': saved['baselines']}, ensure_ascii=False, sort_keys=True))\n",
        "print(json.dumps({'calibration': saved['calibration'], 'rank': saved['rank'], 'monthly': saved['monthly']}, ensure_ascii=False, sort_keys=True))\n",
        "print(json.dumps({'components': saved['components'], 'data_quality': saved['data_quality'], 'leakage': saved['leakage']}, ensure_ascii=False, sort_keys=True))\n",
        "print(json.dumps({'diagnostic_sha256': sha256_file(saved_path), 'primary': saved['primary']['record'], 'recomputed_from_sealed_inputs': True}, sort_keys=True))\n",
    )


def write_executed_notebook(root: Path, output: Path) -> None:
    cells = execute_notebook_cells(
        notebook_sources(root), {"__name__": "__diagnostic_notebook__"}
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
