"""The committed message coverage table must match the equipment's dispatch table."""

from __future__ import annotations

from pathlib import Path

from gemsim.coverage import coverage_markdown, coverage_rows
from gemsim.gem.equipment import HOST_PRIMARIES, GemEquipment

ROOT = Path(__file__).resolve().parents[1]


def test_committed_table_is_up_to_date() -> None:
    committed = (ROOT / "docs" / "message_coverage.md").read_text(encoding="utf-8")
    assert committed == coverage_markdown(), "run: python -m gemsim coverage --write docs/message_coverage.md"


def test_every_listed_host_primary_has_a_handler() -> None:
    for name, method in HOST_PRIMARIES.values():
        assert callable(getattr(GemEquipment, method, None)), f"{name}: no {method}"
    assert ("S2F41", "S2F42", "Host Command Send", "H->E") in coverage_rows()
    assert ("S9F9", "-", "Transaction Timer Timeout", "E->H") in coverage_rows()
