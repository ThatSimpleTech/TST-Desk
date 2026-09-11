"""Signing policy docs stay aligned with v0.1 refusal (TD-4903)."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
README = ROOT / "README.md"
SIGNING = ROOT / "docs" / "signing.md"
DECISIONS = ROOT / "DECISIONS.md"


def test_signing_doc_exists_and_refuses_v01() -> None:
    text = SIGNING.read_text(encoding="utf-8")
    assert "v0.1 ships **unsigned**" in text
    assert "APPLE_CERTIFICATE" in text
    assert "never the repo" in text.lower() or "Never the repo" in text
    assert "telemetry" in text.lower()


def test_readme_points_at_unsigned_section_and_decisions() -> None:
    readme = README.read_text(encoding="utf-8")
    assert "not code-signed or notarized" in readme
    assert "DECISIONS.md" in readme
    assert "Gatekeeper" in readme
    assert "SmartScreen" in readme


def test_decisions_records_td4903() -> None:
    decisions = DECISIONS.read_text(encoding="utf-8")
    assert "TD-4903" in decisions
    assert "unsigned" in decisions.lower()
