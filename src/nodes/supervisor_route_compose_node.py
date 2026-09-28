"""SVC-C2-142 — inner workflow step 3: supervisor_route_compose.

Composes the **SupervisorRoutingBrief** deliverable: a routing summary, and a per-exception routing entry
(policy-defined exception type, candidate supervisor queue with escalation tier + SLA, cited coverage /
policy clauses, needs-review mark), each cited to its source record. Exceptions are ordered by priority
(critical / urgent first). The brief is candidate / advisory only — it never assigns a shift, contacts an
employee, or makes a staffing decision. On the 0-classified / rejected branch it emits the out-of-scope
safe answer.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import ShiftCoverageExceptionService
from src.utils.audit import emit_trace_event

_OUT_OF_SCOPE = (
    "分類可能なシフトカバレッジ例外レコードが入力に見つかりませんでした。"
    "exceptions 配列に exception_id と role_ref・required/available headcount・skills・"
    "notice_minutes・demand/capacity 等を含む JSON をご指定いただくか、対象範囲・期間を明確にしてください。"
)


class SupervisorRouteComposeNode(FunctionNode):
    """Compose the SupervisorRoutingBrief deliverable with citations (or safe answer on 0-classified)."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        classified = json.loads(state.get("classified_exceptions") or "[]")
        if state.get("error_code") or not classified:
            emit_trace_event(
                "supervisor_route_compose.safe", {"reason": state.get("error_code") or "no_classified"}, state
            )
            report: dict[str, Any] = {
                "status_kind": "out_of_scope",
                "message": _OUT_OF_SCOPE,
                "routing_summary": {},
                "routing_briefs": [],
                "citations": [],
            }
            return {"result": json.dumps(report, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

        references = json.loads(state.get("coverage_references") or "{}")
        briefs: list[dict[str, Any]] = []
        citations: list[dict[str, str]] = []
        for c in classified:
            refs = references.get(c["exception_id"], {"coverage_refs": [], "policy_refs": []})
            briefs.append(ShiftCoverageExceptionService.compose_route(c, refs))
            citations.append({"exception_id": c["exception_id"], "source": c["source"]})
        briefs.sort(key=lambda b: (-b["priority_rank"], -b["severity_score"], b["exception_id"]))

        summary = ShiftCoverageExceptionService.routing_summary(briefs)
        report = {
            "status_kind": "supervisor_routing_brief",
            "scope": self._scope(state),
            "routing_summary": summary,
            "routing_briefs": briefs,
            "citations": citations,
        }
        emit_trace_event(
            "supervisor_route_compose.complete",
            {
                "exception_count": len(briefs),
                "queue_count": sum(len(b["candidate_supervisor_queue"]) for b in briefs),
                "citation_count": len(citations),
            },
            state,
        )
        return {"result": json.dumps(report, ensure_ascii=False), "status": AgentStatus.SUCCESS.value}

    @staticmethod
    def _scope(state: dict[str, Any]) -> dict[str, Any]:
        slots = json.loads(state.get("validated_input") or "{}")
        return {"scope": slots.get("scope"), "period": slots.get("period")}
