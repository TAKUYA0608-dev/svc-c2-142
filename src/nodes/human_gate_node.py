"""SVC-C2-142 — inner workflow step 4: human_gate (HumanApprovalGate).

Deterministic human-in-the-loop gate. It does **not** execute anything and it never assigns a supervisor —
it flags the material decisions (critical / urgent exceptions, and any candidate routing that will drive a
shift/staffing action) that require an authorized human supervisor's sign-off before any assignment,
records them + the review status into the brief, and sets ``human_review_required``. Skips (no-op) on the
rejected / 0-classified safe-answer branch (no human gate needed) after emitting a skip audit event.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.utils.audit import emit_trace_event


class HumanGateNode(FunctionNode):
    """Flag material decisions requiring authorized human supervisor approval; set human_review_required."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        report = json.loads(state.get("result") or "{}")
        if (
            state.get("error_code")
            or state.get("classified_count", 0) == 0
            or report.get("status_kind") != "supervisor_routing_brief"
        ):
            emit_trace_event("human_gate.skip", {"reason": state.get("error_code") or "no_brief"}, state)
            return {
                "human_review_required": False,
                "review_status": "not_required",
                "status": AgentStatus.SUCCESS.value,
            }

        material: list[dict[str, Any]] = []
        for brief in report.get("routing_briefs", []):
            # Every candidate routing needs a human before a shift/staffing action; critical/urgent
            # exceptions are always flagged. The agent proposes; it never assigns.
            if (
                brief["is_critical"]
                or brief["exception_type"] == "uncovered_critical_role"
                or brief["candidate_supervisor_queue"]
            ):
                material.append(
                    {
                        "exception_id": brief["exception_id"],
                        "exception_type": brief["exception_type"],
                        "is_critical": brief["is_critical"],
                        "reason": "Candidate supervisor routing / critical exception — requires authorized "
                        "supervisor sign-off before any shift/staffing action",
                    }
                )

        required = bool(material)
        review = {
            "required": required,
            "status": "pending_human_approval" if required else "not_required",
            "note": "Supervisor assignment and any shift/staffing action must be confirmed by an "
            "authorized human supervisor. This agent produces a candidate routing brief only.",
            "material_decisions": material,
        }
        report["human_review"] = review
        emit_trace_event(
            "human_gate.complete", {"review_required": required, "material_decision_count": len(material)}, state
        )
        return {
            "result": json.dumps(report, ensure_ascii=False),
            "human_review_required": required,
            "review_status": review["status"],
            "status": AgentStatus.SUCCESS.value,
        }
