"""SVC-C2-142 — inner workflow step 1: exception_classify.

Deterministic ingest + normalization of the supplied already-reported exception records, then per-record
classification into a policy-defined exception type (uncovered_critical_role / skill_mismatch /
late_absence / demand_capacity_conflict, or unclassified) against the seeded approved coverage matrix, with
matched drivers + evidence. Sets ``classified_count``. **0 valid exceptions (rejected input, non-JSON text,
or all rows missing exception_id) routes to the out-of-scope safe answer** — the agent never fabricates a
classification for data it did not receive. The free-text context is never interpreted semantically, so
prompt-like text in a supplied field cannot influence the classification.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import ShiftCoverageExceptionService
from src.utils.audit import emit_trace_event


class ExceptionClassifyNode(FunctionNode):
    """Ingest + normalize supplied exceptions and classify each into a policy-defined type + drivers."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # Exceptions arrive already validated + provenance-resolved by pre_process (S-1): each `source` is a
        # grounded citation `src:<sha8>` or None (a forged surrogate was dropped at S-1). We do not re-run
        # provenance here — normalize trusts that single upstream resolution.
        slots = json.loads(state.get("validated_input") or state.get("user_input") or "{}")
        if not isinstance(slots, dict):
            slots = {}
        canonical = json.dumps(slots, ensure_ascii=False)
        exceptions = slots.get("exceptions") if isinstance(slots.get("exceptions"), list) else []

        if state.get("error_code") or not exceptions:
            emit_trace_event("exception_classify.skip", {"reason": state.get("error_code") or "no_exceptions"}, state)
            return {
                "validated_input": canonical,
                "classified_exceptions": "[]",
                "classified_count": 0,
                "error_code": state.get("error_code") or "NO_EXCEPTIONS",
                "status": AgentStatus.SUCCESS.value,
            }

        normalized = ShiftCoverageExceptionService.normalize(exceptions)
        if not normalized:
            emit_trace_event("exception_classify.skip", {"reason": "all_malformed"}, state)
            return {
                "validated_input": canonical,
                "classified_exceptions": "[]",
                "classified_count": 0,
                "error_code": "NO_EXCEPTIONS",
                "status": AgentStatus.SUCCESS.value,
            }

        classified = [ShiftCoverageExceptionService.classify(s) for s in normalized]
        distribution: dict[str, int] = {}
        for c in classified:
            distribution[c["exception_type"]] = distribution.get(c["exception_type"], 0) + 1
        emit_trace_event(
            "exception_classify.complete",
            {"supplied": len(exceptions), "classified": len(classified), "type_distribution": distribution},
            state,
        )
        return {
            "validated_input": canonical,
            "classified_exceptions": json.dumps(classified, ensure_ascii=False),
            "classified_count": len(classified),
            "status": AgentStatus.SUCCESS.value,
        }
