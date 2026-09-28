"""SVC-C2-142 — inner domain workflow graph (Cat 2).

Instantiated by ShiftCoverageExceptionRouteWorkflowGraphNode.get_subgraph() in graph.py. Linear topology
with per-node skip guards (the portable Cat 2 form; conditional edges don't propagate across the subgraph
boundary):

    START → exception_classify → coverage_reference_retrieve → supervisor_route_compose → human_gate → END

On rejected / 0-exception input, exception_classify sets classified_count=0 (+error_code);
coverage_reference_retrieve and human_gate no-op and supervisor_route_compose emits the out-of-scope safe
answer — no fabricated brief.
"""

from __future__ import annotations
from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState

from src.nodes.coverage_reference_retrieve_node import CoverageReferenceRetrieveNode
from src.nodes.exception_classify_node import ExceptionClassifyNode
from src.nodes.human_gate_node import HumanGateNode
from src.nodes.supervisor_route_compose_node import SupervisorRouteComposeNode
from src.schemas.state import State


class ShiftCoverageExceptionRouteWorkflow(BaseGraph):
    """Inner graph: exception_classify → coverage_reference_retrieve → supervisor_route_compose → human_gate."""

    @property
    def name(self) -> str:
        return "ShiftCoverageExceptionRouteWorkflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        pass

    def register_nodes(self) -> None:
        # No super() — BaseGraph.register_nodes() is abstract.
        self._nodes["exception_classify"] = ExceptionClassifyNode()
        self._nodes["coverage_reference_retrieve"] = CoverageReferenceRetrieveNode()
        self._nodes["supervisor_route_compose"] = SupervisorRouteComposeNode()
        self._nodes["human_gate"] = HumanGateNode()

    def add_edges(self) -> None:
        # Static linear backbone; the 0-exception / rejected skip is handled by per-node guards.
        self._sg.add_edge(START, "exception_classify")
        self._sg.add_edge("exception_classify", "coverage_reference_retrieve")
        self._sg.add_edge("coverage_reference_retrieve", "supervisor_route_compose")
        self._sg.add_edge("supervisor_route_compose", "human_gate")
        self._sg.add_edge("human_gate", END)

    def route(self, state: AgentState) -> str:
        """Required by the BaseGraph ABC. Linear topology → not wired to a conditional edge."""
        if state.get("error_code") or state.get("classified_count", 0) == 0:
            return "supervisor_route_compose"
        return "coverage_reference_retrieve"

    def get_output(self, state: AgentState) -> dict[str, Any]:
        return {
            "output": state.get("result"),
            "status": state.get("status"),
            "classified_count": state.get("classified_count", 0),
            "human_review_required": state.get("human_review_required", False),
            "error_code": state.get("error_code"),
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
