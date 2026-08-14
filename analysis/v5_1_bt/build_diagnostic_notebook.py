from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mlb_backtest.notebook_artifact import write_executed_notebook  # noqa: E402


write_executed_notebook(
    ROOT, ROOT / "analysis/v5_1_bt/diagnostic_100.ipynb"
)
