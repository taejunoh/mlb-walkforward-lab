from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

import mlb_backtest.protocols as protocols


ROOT = Path(__file__).resolve().parents[1]

SOURCE_FINALIZATION_DOCS = (
    ROOT
    / "docs/superpowers/specs/2026-07-19-mlb-v5-2-source-finalization-cli-design.md",
    ROOT
    / "docs/superpowers/plans/2026-07-20-mlb-v5-2-source-finalization-cli.md",
)
DIAGNOSTIC_PLAN = (
    ROOT / "docs/superpowers/plans/2026-07-12-mlb-v5-2-diagnostic-and-500-pick.md"
)
CONTINUATION_AMENDMENT = (
    ROOT / "docs/superpowers/specs/2026-07-23-v5-2-downstream-continuation-amendment.md"
)
TASK12_CONTINUATION_DOCS = (DIAGNOSTIC_PLAN, CONTINUATION_AMENDMENT)
PINNED_DESIGN = (
    ROOT / "docs/superpowers/specs/2026-07-12-mlb-v5-2-diagnostic-and-500-pick-design.md"
)


def _normalized_text(path: Path) -> str:
    """Compare prose semantics while ignoring Markdown line wrapping."""
    return re.sub(r"\s+", " ", path.read_text(encoding="utf-8")).strip()


def _task12_amendment(path: Path) -> str:
    """Return only the Task 12 amendment section from a source authority doc."""
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^##\s+(?:\d+\.\s+)?Task 12 downstream-continuation amendment\b.*?(?=^##\s+|\Z)",
        text,
    )
    if match is None:
        raise AssertionError(f"{path.name} has no Task 12 downstream amendment section")
    return re.sub(r"\s+", " ", match.group(0)).strip()


class V52DownstreamDocumentationTest(unittest.TestCase):
    def test_source_finalization_docs_bind_superseding_authority_without_execution(self) -> None:
        """Finalization amendments must be explicit in both source-workflow docs."""
        required_clauses = (
            "namespace-parametric",
            "Their old-run behavior and receipt selection semantics remain unchanged.",
            "must not select a receipt by glob, timestamp, or \u201clatest\u201d convention",
        )
        for path in SOURCE_FINALIZATION_DOCS:
            text = _task12_amendment(path)
            for clause in required_clauses:
                with self.subTest(path=path.name, clause=clause):
                    self.assertIn(clause, text)

    def test_continuation_docs_gate_only_superseding_sources_ready_and_propagate_projection(self) -> None:
        """Later fitting/reporting remains blocked until the selected source run is ready."""
        required_clauses = (
            "only a `SOURCES_READY` source inventory bound to the superseding run may be considered",
            "the old run remains blocked",
            "exact neutralization projection",
            "No Plan B/C artifact is created",
            "may not silently select a \"latest\" registry, inventory, review, replay, or source root",
        )
        for path in TASK12_CONTINUATION_DOCS:
            text = (
                _task12_amendment(path)
                if path == DIAGNOSTIC_PLAN
                else _normalized_text(path)
            )
            for clause in required_clauses:
                with self.subTest(path=path.name, clause=clause):
                    self.assertIn(clause, text)

    def test_task12_sections_bind_exact_superseding_disposition_authority(self) -> None:
        """Every source authority must state the complete exact-authority contract."""
        expected = (
            "The exact superseding source authority binds "
            "`historical_source_acquisition_superseding_565534.json` to its "
            "authenticated exact disposition sidecar authority."
        )
        for path in SOURCE_FINALIZATION_DOCS:
            with self.subTest(path=path.name):
                self.assertIn(expected, _task12_amendment(path))

    def test_task12_sections_do_not_assert_sources_ready_and_require_reapproval(self) -> None:
        """No source-ready result is claimed; all downstream work needs a new plan."""
        not_asserted = re.compile(
            r"(?:No `SOURCES_READY` inventory is asserted to exist|"
            r"`SOURCES_READY` is not claimed to exist(?: here| in this amendment)?)"
        )
        downstream_reapproval = re.compile(
            r"Fit, OOF, cards, targets, outcomes, and 500-pick work "
            r"(?:remain subject to|require) a separately approved plan "
            r"that explicitly selects the superseding run\."
        )
        for path in TASK12_CONTINUATION_DOCS:
            section = (
                _task12_amendment(path)
                if path == DIAGNOSTIC_PLAN
                else _normalized_text(path)
            )
            with self.subTest(path=path.name, contract="SOURCES_READY"):
                self.assertRegex(section, not_asserted)
            with self.subTest(path=path.name, contract="downstream reapproval"):
                self.assertRegex(section, downstream_reapproval)

    def test_approved_7_12_design_pin_is_unchanged(self) -> None:
        """Task 12 is documentation-only; the approved 7/12 design blob stays pinned."""
        approved = protocols.approved_design_bytes(ROOT)
        self.assertEqual(
            hashlib.sha256(approved).hexdigest(), protocols.CURRENT_APPROVED_DESIGN_SHA256
        )
        self.assertEqual(PINNED_DESIGN.read_bytes(), approved)


if __name__ == "__main__":
    unittest.main()
