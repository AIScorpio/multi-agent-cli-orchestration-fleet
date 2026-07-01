"""Phase 5.5 — SKILL.md documents the leader-emits-phases.json lifecycle, so the SOP exists in
the one place the leader reads: init scaffolds awaiting_definition → leader fills (set_phases) or
marks no_pipeline; fleet stays mission-agnostic."""
from pathlib import Path

SKILL = Path(__file__).resolve().parents[2] / "SKILL.md"


def test_skill_documents_phase_manifest_lifecycle():
    low = SKILL.read_text().lower()
    assert "awaiting_definition" in low, "SOP must name the agnostic initial state"
    assert "fills it" in low and "leader" in low, "SOP must say the leader fills the manifest"
    assert "set_phases" in low, "SOP must point at the fill API"
    assert "no_pipeline" in low, "SOP must cover the flat one-shot project case"
    # leader is the SOLE author; pipeline provides only a template; deriver syncs status only
    assert "sole author" in low, "SOP must state the leader is the sole author of the definitions"
    assert "only a template" in low, "SOP must state a pipeline provides only a template"
    assert "never writes `phases.json`" in low or "never writes phases.json" in low, \
        "SOP must state the pipeline never writes phases.json itself"
