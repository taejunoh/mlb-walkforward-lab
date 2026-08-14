"""Retired Task 2a entry point; runtime execution moved to the root helper."""

from __future__ import annotations

import sys


def main() -> int:
    raise RuntimeError("R4_TASK2A_DEPRECATED_NO_EXECUTION")


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
