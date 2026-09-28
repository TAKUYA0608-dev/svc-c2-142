"""SVC-C2-142 — Agent state (Service Shift-Coverage Exception Router, Cat 2).

ADR-005: State is a flat TypedDict — never a validation/BaseModel instance. Complex fields are stored
as JSON strings (``NotRequired[str]`` + ``# JSON:``); nodes ``json.dumps`` on write / ``json.loads`` on read.

Read-only / advisory: the agent ingests an already-reported shift-coverage exception record (pseudonymous
identifiers + free-text context), classifies it against the approved coverage matrix + escalation policy,
and produces a **SupervisorRoutingBrief** deliverable — it never creates or changes a shift, contacts an
employee, infers availability or a protected attribute, approves overtime, or makes a staffing decision.
The final shift/staffing decision is always an authorized human supervisor's, and the router output is
candidate / needs-review only.

All agent-specific fields are NotRequired (populated progressively; absent at empty-start invoke).
"""

from __future__ import annotations


from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Agent state for the shift-coverage exception classification + supervisor-routing workflow."""

    # ── pre_process (ExceptionRecordIngest + SensitiveDataMinimise; S-1 + S-2 pre-LLM) ──
    validated_input: str  # JSON: {exceptions[], scope, period} (PII/protected fields minimised)
    input_format: str  # "json" | "text" | "empty" | "rejected"
    enriched_context: str  # JSON: {source, channel} (read-only caller context)

    # ── inner workflow (exception_classify → coverage_reference_retrieve → supervisor_route_compose → human_gate) ─
    classified_exceptions: str  # JSON: [{exception_id, exception_type, matched_drivers[], source}]
    classified_count: int  # exceptions classified (0 → out-of-scope safe answer)
    coverage_references: str  # JSON: {exception_id: {coverage_refs[], policy_refs[]}}
    result: str  # JSON: assembled SupervisorRoutingBrief (incl. human_review)
    human_review_required: bool  # True once the HumanApprovalGate flags material decisions
    review_status: str  # "pending_human_approval" | "not_required"

    # ── post_process (OutputSanitise — S-3 gate + S-4 audit) ──────────────────
    formatted_output: str  # JSON: final response envelope (brief + disclaimer)
    disclaimer: str  # mandatory DRAFT / advisory-only disclaimer
    audit_logged: bool  # True once the terminal audit event is emitted

    # ── degraded-path signalling (SUCCESS + error_code, never status=ERROR) ───
    # INPUT_REJECTED | INJECTION_REJECTED | INPUT_TOO_LONG | NO_EXCEPTIONS | CITATION_INCOMPLETE
    error_code: str
    error_message: str  # operator-facing detail
