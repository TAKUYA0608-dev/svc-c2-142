# SVC-C2-142 — Integration: full outer Graph().invoke() across a multi-exception portfolio

import json

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel

from src.graph.graph import Graph

_SUCCESS = AgentStatus.SUCCESS.value


def _invoke(user_input: str):
    ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
    return Graph().invoke(user_input, ctx=ctx)


def test_multi_exception_portfolio_prioritised_and_grounded():
    payload = {
        "scope": "apac-night",
        "exceptions": [
            {"exception_id": "e1", "role_ref": "cashier", "available_headcount": 2,
             "available_skills": ["pos"], "demand_index": 160, "scheduled_capacity": 100,
             "source": "roster:e1"},                                     # demand_capacity_conflict
            {"exception_id": "e2", "role_ref": "rn_er", "available_headcount": 1,
             "available_skills": ["basic"], "notice_minutes": 30, "source": "wfm:e2"},  # critical + more
            {"exception_id": "e3", "role_ref": "support_l1", "available_headcount": 2,
             "available_skills": [], "source": "scheduling:e3"},         # skill_mismatch
        ],
    }
    out = _invoke(json.dumps(payload))
    assert out["status"] == _SUCCESS
    env = json.loads(out["output"])
    assert env["status_kind"] == "supervisor_routing_brief"
    assert len(env["routing_briefs"]) == 3 and len(env["citations"]) == 3
    # critical / urgent exception is ranked first
    assert env["routing_briefs"][0]["exception_type"] == "uncovered_critical_role"
    # summary rolls up the type distribution and the urgent-review list
    assert env["routing_summary"]["total_exceptions"] == 3
    assert env["routing_briefs"][0]["exception_id"] in env["routing_summary"]["exceptions_needing_urgent_review"]
    assert env["human_review"]["required"] is True
    assert "DRAFT" in env["disclaimer"]


def test_mixed_cited_and_uncited_blocks_whole_brief():
    """Per-exception citation completeness: one uncited exception fails-closed the whole grounded brief."""
    payload = {"exceptions": [
        {"exception_id": "e1", "role_ref": "rn_er", "available_headcount": 0, "source": "wfm:e1"},  # cited
        {"exception_id": "e2", "role_ref": "cashier", "available_headcount": 0},                     # uncited
    ]}
    out = _invoke(json.dumps(payload))
    env = json.loads(out["output"])
    assert env["status_kind"] == "needs_review"
    assert env["routing_briefs"] == [] and env["citations"] == []


def test_forged_exception_id_surrogate_rehashed():
    # ★ F-02: a caller value SHAPED like an internal surrogate (exc:deadbeef) is re-hashed at S-1 (no
    # syntactic passthrough), so it can never forge an internal join key / reference another exception.
    payload = {"exceptions": [
        {"exception_id": "exc:deadbeef", "role_ref": "rn_er", "available_headcount": 0, "source": "wfm:e1"},
    ]}
    out = _invoke(json.dumps(payload))
    env = json.loads(out["output"])
    assert env["status_kind"] == "supervisor_routing_brief"
    tok = env["routing_briefs"][0]["exception_id"]
    assert tok.startswith("exc:") and tok != "exc:deadbeef"   # re-hashed, not passthrough
    assert "exc:deadbeef" not in out["output"]


def test_empty_object_is_out_of_scope():
    out = _invoke(json.dumps({"exceptions": []}))
    env = json.loads(out["output"])
    assert env["status_kind"] == "out_of_scope" and env["citations"] == []
