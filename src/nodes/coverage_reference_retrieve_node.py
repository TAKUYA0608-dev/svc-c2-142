"""SVC-C2-142 — inner workflow step 2: coverage_reference_retrieve.

Deterministically retrieves the cited coverage-matrix + escalation-policy clauses for each classified
exception (``<clause_id>@<version>`` from the seeded approved policy) so the routing brief is grounded in
authorized clauses. Skips (no-op) on rejected / 0-classified input, after emitting a skip audit event.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import ShiftCoverageExceptionService
from src.utils.audit import emit_trace_event


class CoverageReferenceRetrieveNode(FunctionNode):
    """Retrieve cited coverage-matrix + escalation-policy clauses per classified exception."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("error_code") or state.get("classified_count", 0) == 0:
            emit_trace_event(
                "coverage_reference_retrieve.skip", {"reason": state.get("error_code") or "no_classified"}, state
            )
            return {}

        classified = json.loads(state.get("classified_exceptions") or "[]")
        references = {c["exception_id"]: ShiftCoverageExceptionService.retrieve_references(c) for c in classified}
        emit_trace_event(
            "coverage_reference_retrieve.complete",
            {
                "exceptions": len(references),
                "coverage_ref_count": sum(len(r["coverage_refs"]) for r in references.values()),
                "policy_ref_count": sum(len(r["policy_refs"]) for r in references.values()),
            },
            state,
        )
        return {"coverage_references": json.dumps(references, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}
