"""
Task and result data contracts for the multi-agent pipeline.
All fields are validated on construction — invalid specs never reach the queue.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Optional, List
from pathlib import Path
from datetime import datetime
import json, uuid

VALID_TYPES  = {"research", "code", "test", "write", "review"}
VALID_AGENTS = {"kimi", "codex", "opencode", "claude", "any"}
VALID_STATUS = {"COMPLETED", "PARTIAL", "FAILED"}
VALID_CONF   = {"HIGH", "MEDIUM", "LOW"}


def _pipeline_phase_keys():
    """Valid pipeline-phase keys from this project's .fleet/phases.json — the phase ids AND their bare
    numbers (e.g. {"P4", "4"}). Returns None when the project has NO pipeline (phases.json absent), in
    which case phase validation is a NO-OP (mission-agnostic projects are unaffected).

    Every task MUST map to a pipeline phase. Validating this at TaskSpec construction makes an
    orphan-phase task unconstructable (prevention at the source), so the kanban's phase line and its
    per-phase task buckets can never silently desync — instead of detecting+alerting after the fact.
    """
    pf = Path(__file__).resolve().parent / "phases.json"
    if not pf.exists():
        return None
    try:
        phases = json.loads(pf.read_text()).get("phases", [])
    except (ValueError, OSError):
        return None
    ids = {str(p.get("id", "")) for p in phases if p.get("id")}
    if not ids:
        return None
    return ids | {(i[1:] if i.startswith("P") else i) for i in ids}


# ── Task ────────────────────────────────────────────────────────────────────

@dataclass
class TaskSpec:
    title: str
    phase: str                         # e.g. "1" or "Phase 1 — Literature"
    type: str                          # research | code | test | write | review
    description: str
    assigned_to: str                   # kimi | codex | opencode | claude | any
    output_file: str                   # path relative to workspace root
    acceptance_criteria: List[str]
    context_files: List[str] = field(default_factory=list)  # paths relative to workspace root
    priority: int = 5                  # 1 = highest urgency, 10 = lowest
    # Task-level dependency DAG — the real scheduler (phases are display/advisory).
    # Each entry is either a concrete task_id ("task-ab12cd34") or the sugar
    # "phase:<id>" meaning "every task with that phase is QA-passed". A held draft
    # is released to pending the moment ALL its depends_on are satisfied
    # (producer QA-passed AND its output_file present). Empty = runnable now —
    # PARALLEL BY DEFAULT; serialize only by declared necessity.
    depends_on: List[str] = field(default_factory=list)
    # Write-scope globs for collision serialization (P3). Empty → the resolver
    # uses [output_file]. Declare wider scope (e.g. ["src/**"]) when a task edits
    # more than its single output_file so overlapping writers are serialized.
    write_scope: List[str] = field(default_factory=list)
    # Machine-checkable acceptance predicates (P4/P6) enforced at qa-pass by the
    # mechanical floor: scalar/regex/command (see predicates.py). Empty = none.
    acceptance_predicates: List[dict] = field(default_factory=list)
    task_id: str = field(default_factory=lambda: f"task-{uuid.uuid4().hex[:8]}")
    created_by: str = "claude-orchestrator"
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    retry_of: Optional[str] = None
    retry_reason: Optional[str] = None
    rerouted_from: Optional[str] = None   # agent this task was quota-rerouted away from
    stuck_count: int = 0                   # hung-child requeues (cap MAX_STUCK)
    orphan_count: int = 0                  # dead-claimer requeues (cap MAX_REQUEUE) — SEPARATE
    qa_fail_count: int = 0                 # QA-fail retries in this lineage; capped (MAX_QA_FAIL)

    def __post_init__(self):
        if self.type not in VALID_TYPES:
            raise ValueError(f"type must be one of {VALID_TYPES}, got '{self.type}'")
        if self.assigned_to not in VALID_AGENTS:
            raise ValueError(f"assigned_to must be one of {VALID_AGENTS}, got '{self.assigned_to}'")
        if not 1 <= self.priority <= 10:
            raise ValueError(f"priority must be 1-10, got {self.priority}")
        if not self.acceptance_criteria:
            raise ValueError("acceptance_criteria must not be empty")
        if not isinstance(self.depends_on, list) or \
                not all(isinstance(d, str) for d in self.depends_on):
            raise ValueError("depends_on must be a list of strings (task ids or 'phase:<id>')")
        _phk = _pipeline_phase_keys()
        if _phk is not None and str(self.phase) not in _phk:
            raise ValueError(
                f"phase '{self.phase}' is not a defined pipeline phase. EVERY task must map to a "
                f"pipeline phase ({', '.join(sorted(_phk))}). Tag it with an existing phase, OR first "
                f"define a new phase in .fleet/phases.json and then create the task."
            )

    # Filename used in all queue directories
    @property
    def filename(self) -> str:
        return f"{self.task_id}.json"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_path(cls, path: Path) -> TaskSpec:
        data = json.loads(path.read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


# ── Result ──────────────────────────────────────────────────────────────────

@dataclass
class TaskResult:
    task_id: str
    status: str                        # COMPLETED | PARTIAL | FAILED
    agent: str
    completed_at: str
    deliverable: str                   # extracted main content from LLM output
    output_file: str
    confidence: str                    # HIGH | MEDIUM | LOW
    criteria_met: List[str] = field(default_factory=list)  # ["1:YES", "2:NO", ...]
    notes: str = ""
    error: str = ""

    def __post_init__(self):
        if self.status not in VALID_STATUS:
            raise ValueError(f"status must be one of {VALID_STATUS}, got '{self.status}'")
        if self.confidence not in VALID_CONF:
            raise ValueError(f"confidence must be one of {VALID_CONF}, got '{self.confidence}'")

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_path(cls, path: Path) -> TaskResult:
        data = json.loads(path.read_text())
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
