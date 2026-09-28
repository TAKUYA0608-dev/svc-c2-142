"""SVC-C2-142 — deterministic domain services (no framework imports, no LLM).

ShiftCoverageExceptionService: normalizes an already-reported shift-coverage exception record into a
canonical signal set (coverage gap, skill gap, notice lateness, demand-capacity gap, criticality),
classifies it into a policy-defined exception type (uncovered_critical_role / skill_mismatch /
late_absence / demand_capacity_conflict) against the seeded, approved role/skill coverage matrix,
retrieves the cited coverage-matrix / escalation-policy clauses, and composes a candidate supervisor
routing queue.

Everything here is deterministic and auditable (threshold banding + set membership + keyed clause
composition) — there is **no LLM** (no model in config/agent.yaml, no LLM dependency in pyproject, no LLM
call anywhere in src/). Records are keyed by an opaque, non-reversible ``exception_id`` surrogate; the raw
staff reference and any free-text context are never carried into the routing brief, and the S-3 output gate
re-redacts anything that leaks. Seeded coverage matrix / escalation policy are overridable by CoE (a
change-controlled engineer MR + specialist review) without touching node logic.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Two SEPARATE concerns — do not conflate them:
#   (1) PRIVACY (opaque_id): every caller identifier (exception_id / staff_ref / site_ref) is
#       UNCONDITIONALLY tokenized to a deterministic, non-reversible opaque surrogate so labour PII (even a
#       bare name like ``Alice`` / ``Taro.Yamada`` / ``TaroYamada``, no spaces/symbols) can never reach a
#       citation or the routing brief. Tokenizing is a privacy measure — it does NOT assert the value is
#       authorized/verifiable. Surrogates are one-way hashes; graph state never stores a surrogate→raw
#       rejoin map, so the opaque ID is non-linkable back to the person.
#   (2) PROVENANCE (resolve_provenance): a caller ``source`` becomes a grounded CITATION only when it is
#       resolvable against the authorized provenance registry (names a trusted workforce system of record).
#       Any other free text (a staff name, ``unknown``, a fabricated value, or a caller value merely SHAPED
#       like a surrogate ``src:1a2b3c4d``) is NOT verifiable provenance → it yields NO citation → S-3 blocks
#       the brief as CITATION_INCOMPLETE (fail-closed). "Tokenized" is never sufficient for a citation.
# Tokenization is UNCONDITIONAL (no syntactic passthrough): a caller value merely *shaped* like a surrogate
# (``exc:deadbeef``) is re-hashed, never trusted, so it can never forge an internal join key. Identifiers /
# provenance are resolved exactly once at S-1 (pre_process); downstream trusts that resolution verbatim.
_SAFE_TOKEN = re.compile(r"^[a-z0-9_\-]{1,48}$")

# Authorized provenance registry: the workforce / scheduling systems of record a service operator trusts as
# verifiable data sources. A caller ``source`` is accepted as a grounded citation ONLY when its leading
# namespace names one of these (the "trusted context"). This is the deploying org's / CoE's registry —
# overridable without touching node logic; it is a SEMANTIC allowlist of authorized systems, not a syntactic
# character class.
AUTHORIZED_PROVENANCE_SYSTEMS = frozenset(
    {
        "wfm",
        "workforce",
        "workforce_management",
        "scheduling",
        "scheduler",
        "roster",
        "rostering",
        "shift_system",
        "shiftsystem",
        "timeclock",
        "time_and_attendance",
        "attendance",
        "hris",
        "hcm",
        "workday",
        "sap_successfactors",
        "employee_system",
        "itsm",
        "service_desk",
        "ops_console",
        "incident_system",
        "system_of_record",
        "sor",
        "authorized_feed",
        "coverage_feed",
        "exception_feed",
    }
)


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def opaque_id(value: Any, prefix: str) -> str:
    """PRIVACY tokenize a caller identifier to a deterministic, non-reversible opaque surrogate
    ``<prefix>:<sha8>``.

    Caller identifiers are **always** tokenized — no syntactic passthrough — so a labour name (with or
    without spaces) can never survive into a citation or the brief, and a caller value merely *shaped* like a
    surrogate (``exc:deadbeef``) is re-hashed rather than trusted (it can never forge an internal join key).
    Same input → same surrogate (brief / citations / summary stay joinable within one invocation). This is a
    privacy measure only; it makes no claim that the identifier is authorized, and no surrogate→raw rejoin
    map is ever kept.
    """
    return f"{prefix}:{_sha8(str(value or '').strip())}"


def resolve_provenance(value: Any) -> str | None:
    """Resolve a **raw** caller ``source`` to a grounded, privacy-tokenized CITATION — or ``None``.

    Provenance validation (separate from privacy) and the **single** resolution point (S-1 / pre_process).
    A citation is emitted **only** when the source names an authorized workforce system of record
    (``<authorized-namespace>[:<ref>]``). Any other value — a staff name, ``unknown``, a fabricated value,
    **or a value that merely looks like a surrogate (``src:1a2b3c4d``)** — is not verifiable provenance and
    returns ``None`` so the S-3 gate blocks the brief as CITATION_INCOMPLETE (fail-closed). When authorized,
    the raw label is never used verbatim: the citation is a privacy hash (``src:<sha8>``) of the authorized
    reference. No synthetic provenance is fabricated.

    ★ Forged-surrogate defence: there is **no format-based passthrough**. A caller-supplied ``src:<hex>``
    has namespace ``src`` (not an authorized system of record), so it resolves to ``None`` — it is dropped
    here at S-1 and can never reach a citation. Because provenance is resolved exactly once (here), the
    produced ``src:<sha8>`` is the trusted citation downstream and is **never** fed back through this
    function (which would, correctly, reject it), so no forged value can imitate an internal surrogate.
    """
    text = str(value or "").strip()
    if not text:
        return None
    namespace = text.split(":", 1)[0].strip().lower()
    if namespace not in AUTHORIZED_PROVENANCE_SYSTEMS:
        return None  # unverifiable / forged-surrogate provenance → fail-closed (no citation → needs_review)
    return "src:" + _sha8(text)


# ── seeded exception taxonomy: policy-defined type → human-readable description ──
EXCEPTION_TAXONOMY: dict[str, str] = {
    "uncovered_critical_role": "A business-critical role has an uncovered headcount gap for the shift",
    "skill_mismatch": "Available staff do not hold the required skills/certifications for the role",
    "late_absence": "A coverage-affecting absence was reported inside the short-notice window",
    "demand_capacity_conflict": "Forecast demand for the shift exceeds the scheduled capacity",
    "unclassified": "No policy-defined exception type matched; routed for manual supervisor review",
}

# ── seeded, approved role/skill coverage matrix (authorized clauses, CoE-calibratable) ──
# clause_id@version is a stable, citable reference to the approved matrix clause for each role.
COVERAGE_MATRIX: dict[str, dict[str, Any]] = {
    "rn_er": {
        "label": "ER Registered Nurse",
        "min_headcount": 3,
        "required_skills": ["acls", "triage"],
        "is_critical": True,
        "clause_id": "CM-ROLE-RN-ER",
        "version": "v3",
    },
    "security": {
        "label": "Security Guard",
        "min_headcount": 1,
        "required_skills": ["guard_license"],
        "is_critical": True,
        "clause_id": "CM-ROLE-SEC",
        "version": "v3",
    },
    "duty_pharmacist": {
        "label": "Duty Pharmacist",
        "min_headcount": 1,
        "required_skills": ["dispensing_license"],
        "is_critical": True,
        "clause_id": "CM-ROLE-PHARM",
        "version": "v3",
    },
    "cashier": {
        "label": "Cashier",
        "min_headcount": 2,
        "required_skills": ["pos"],
        "is_critical": False,
        "clause_id": "CM-ROLE-CASHIER",
        "version": "v3",
    },
    "support_l1": {
        "label": "L1 Support",
        "min_headcount": 4,
        "required_skills": ["helpdesk"],
        "is_critical": False,
        "clause_id": "CM-ROLE-SUP-L1",
        "version": "v3",
    },
    "concierge": {
        "label": "Concierge",
        "min_headcount": 1,
        "required_skills": ["guest_service"],
        "is_critical": False,
        "clause_id": "CM-ROLE-CONCIERGE",
        "version": "v3",
    },
}

# ── seeded, organisation-owned escalation policy (authorized clauses, CoE-calibratable) ──
# Each entry names the candidate supervisor queue + escalation tier + response SLA for an exception type.
# ``supervisor_queue`` is a policy-defined role queue, never a named person; the owner is confirmed by a
# human supervisor.
ESCALATION_POLICY: dict[str, dict[str, Any]] = {
    "uncovered_critical_role": {
        "supervisor_queue": "duty_manager_oncall",
        "escalation_tier": "tier2_urgent",
        "sla_minutes": 15,
        "clause_id": "EP-UCR-01",
        "version": "v2",
        "description": "Uncovered critical role → on-call duty manager, urgent",
    },
    "skill_mismatch": {
        "supervisor_queue": "shift_supervisor",
        "escalation_tier": "tier1_standard",
        "sla_minutes": 60,
        "clause_id": "EP-SKM-01",
        "version": "v2",
        "description": "Skill/certification mismatch → shift supervisor for reassignment",
    },
    "late_absence": {
        "supervisor_queue": "shift_supervisor",
        "escalation_tier": "tier1_standard",
        "sla_minutes": 30,
        "clause_id": "EP-LAB-01",
        "version": "v2",
        "description": "Late-notice absence → shift supervisor for interim cover",
    },
    "demand_capacity_conflict": {
        "supervisor_queue": "workforce_planner",
        "escalation_tier": "tier1_standard",
        "sla_minutes": 120,
        "clause_id": "EP-DCC-01",
        "version": "v2",
        "description": "Demand exceeds capacity → workforce planner review",
    },
    "unclassified": {
        "supervisor_queue": "shift_supervisor",
        "escalation_tier": "tier1_standard",
        "sla_minutes": 120,
        "clause_id": "EP-UNCLASS-01",
        "version": "v2",
        "description": "Unclassified exception → shift supervisor for manual review",
    },
}

_LATE_NOTICE_MINUTES = 120  # absence reported within this window before the shift = "late"
_SEVERITY_WEIGHT = {"high": 2, "med": 1}
# Priority ordering for the brief (critical/urgent first).
_TYPE_RANK = {
    "uncovered_critical_role": 4,
    "skill_mismatch": 3,
    "late_absence": 2,
    "demand_capacity_conflict": 1,
    "unclassified": 0,
}
# Deterministic priority in which a matched type becomes the *primary* exception_type.
_TYPE_PRIORITY = ("uncovered_critical_role", "skill_mismatch", "late_absence", "demand_capacity_conflict")


def _num(value: Any, default: float = 0.0) -> float:
    """Coerce to float; non-numeric → default."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class ShiftCoverageExceptionService:
    """Deterministic normalization, exception classification, clause retrieval, and route composition."""

    # ── normalization ────────────────────────────────────────────────────────
    @staticmethod
    def normalize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate + canonicalize reported exception records into a signal set. Rows without an
        ``exception_id`` are dropped.

        The raw staff reference and free-text context are intentionally reduced to opaque IDs / presence
        flags — they are never carried into the signal set that feeds the routing brief. ``exception_id`` is
        always privacy-tokenized. ``source`` was already resolved to a grounded citation (``src:<sha8>``) or
        ``None`` by pre_process (S-1), the single provenance-resolution point — a forged surrogate was
        dropped there. normalize trusts that value verbatim; it never re-resolves and never fabricates
        provenance.
        """
        out: list[dict[str, Any]] = []
        for raw in records or []:
            if not isinstance(raw, dict):
                continue
            raw_id = str(raw.get("exception_id") or raw.get("id") or "").strip()
            if not raw_id:
                continue
            # Tokenize unconditionally (no forgeable passthrough). pre_process (S-1) already tokenized
            # exception_id to an opaque surrogate; re-tokenizing it here is a deterministic no-op-in-effect
            # (exception_id is a per-brief label joined only within this invocation's own output — briefs and
            # citations are both built from this value, so they stay consistent — never against the S-1 value).
            exception_id = opaque_id(raw_id, "exc")
            source = raw.get("source")  # already resolved (src:<sha8> or None) at S-1

            role_raw = str(raw.get("role_ref") or "").strip().lower()
            role_ref = role_raw if _SAFE_TOKEN.match(role_raw) else "unknown"
            matrix = COVERAGE_MATRIX.get(role_ref)

            required = _int(matrix["min_headcount"]) if matrix else _int(raw.get("required_headcount"))
            available = _int(raw.get("available_headcount"))
            required_skills = (
                list(matrix["required_skills"])
                if matrix
                else [
                    str(s).strip().lower()
                    for s in (raw.get("required_skills") or [])
                    if _SAFE_TOKEN.match(str(s).strip().lower())
                ]
            )
            available_skills = {
                str(s).strip().lower()
                for s in (raw.get("available_skills") or [])
                if _SAFE_TOKEN.match(str(s).strip().lower())
            }
            is_critical = bool(matrix["is_critical"]) if matrix else bool(raw.get("is_critical", False))

            notice_minutes = _int(raw.get("notice_minutes")) if raw.get("notice_minutes") is not None else None
            demand_index = _num(raw.get("demand_index"))
            scheduled_capacity = _num(raw.get("scheduled_capacity"))

            out.append(
                {
                    "exception_id": exception_id,
                    "role_ref": role_ref,
                    "role_label": matrix["label"] if matrix else "unspecified_role",
                    "role_in_matrix": matrix is not None,
                    "is_critical": is_critical,
                    "required_headcount": required,
                    "available_headcount": available,
                    "coverage_gap": max(0, required - available),
                    "required_skills": sorted(required_skills),
                    "missing_skills": sorted(s for s in required_skills if s not in available_skills),
                    "notice_minutes": notice_minutes,
                    "demand_gap": round(max(0.0, demand_index - scheduled_capacity), 3),
                    "scheduled_capacity": round(scheduled_capacity, 3),
                    "source": source,
                }
            )
        return out

    # ── classification ───────────────────────────────────────────────────────
    @staticmethod
    def classify(signals: dict[str, Any]) -> dict[str, Any]:
        """Classify one normalized exception into a policy-defined type + matched drivers (with evidence).

        Deterministic threshold/set-membership matching only — the free-text context is never interpreted
        semantically, so prompt-like text in a supplied field can never influence the classification.
        """
        matched: list[dict[str, str]] = []
        gap = signals["coverage_gap"]

        if gap > 0 and signals["is_critical"]:
            matched.append(
                {
                    "exception_type": "uncovered_critical_role",
                    "severity": "high",
                    "evidence": f"critical role, coverage_gap={gap}",
                }
            )
        if signals["missing_skills"]:
            sev = "high" if signals["is_critical"] else "med"
            matched.append(
                {
                    "exception_type": "skill_mismatch",
                    "severity": sev,
                    "evidence": f"missing_skills={signals['missing_skills']}",
                }
            )
        notice = signals["notice_minutes"]
        if notice is not None and notice < _LATE_NOTICE_MINUTES and gap > 0:
            sev = "high" if gap >= max(1, signals["required_headcount"]) else "med"
            matched.append(
                {
                    "exception_type": "late_absence",
                    "severity": sev,
                    "evidence": f"notice_minutes={notice}, coverage_gap={gap}",
                }
            )
        if signals["demand_gap"] > 0:
            cap = signals["scheduled_capacity"]
            sev = "high" if (cap > 0 and signals["demand_gap"] >= 0.25 * cap) else "med"
            matched.append(
                {
                    "exception_type": "demand_capacity_conflict",
                    "severity": sev,
                    "evidence": f"demand_gap={signals['demand_gap']}, capacity={cap}",
                }
            )

        primary = next((t for t in _TYPE_PRIORITY if any(m["exception_type"] == t for m in matched)), "unclassified")
        severity_score = sum(_SEVERITY_WEIGHT[m["severity"]] for m in matched)

        return {
            "exception_id": signals["exception_id"],
            "role_ref": signals["role_ref"],
            "role_label": signals["role_label"],
            "is_critical": signals["is_critical"],
            "exception_type": primary,
            "matched_drivers": matched,
            "severity_score": severity_score,
            "priority_rank": _TYPE_RANK[primary],
            "source": signals["source"],
        }

    # ── clause retrieval ───────────────────────────────────────────────────────
    @staticmethod
    def retrieve_references(classified: dict[str, Any]) -> dict[str, list[str]]:
        """Deterministically retrieve the cited coverage-matrix + escalation-policy clauses for one
        classified exception. Clause refs are ``<clause_id>@<version>`` from the seeded authorized policy."""
        coverage_refs: list[str] = []
        matrix = COVERAGE_MATRIX.get(classified["role_ref"])
        if matrix:
            coverage_refs.append(f"{matrix['clause_id']}@{matrix['version']}")

        policy = ESCALATION_POLICY.get(classified["exception_type"], ESCALATION_POLICY["unclassified"])
        policy_refs = [f"{policy['clause_id']}@{policy['version']}"]
        return {"coverage_refs": coverage_refs, "policy_refs": policy_refs}

    # ── route composition ──────────────────────────────────────────────────────
    @staticmethod
    def compose_route(classified: dict[str, Any], refs: dict[str, list[str]]) -> dict[str, Any]:
        """Compose the per-exception candidate supervisor routing entry (needs-review, cited).

        The candidate queue is a policy-defined role queue, never a named person, and the entry is a
        candidate only — the final shift/staffing decision defers to a human supervisor.
        """
        policy = ESCALATION_POLICY.get(classified["exception_type"], ESCALATION_POLICY["unclassified"])
        candidate_queue = [
            {
                "supervisor_queue": policy["supervisor_queue"],
                "escalation_tier": policy["escalation_tier"],
                "sla_minutes": policy["sla_minutes"],
                "reason": policy["description"],
            }
        ]
        return {
            "exception_id": classified["exception_id"],
            "exception_type": classified["exception_type"],
            "exception_description": EXCEPTION_TAXONOMY.get(classified["exception_type"], ""),
            "role_label": classified["role_label"],
            "is_critical": classified["is_critical"],
            "severity_score": classified["severity_score"],
            "priority_rank": classified["priority_rank"],
            "matched_drivers": classified["matched_drivers"],
            "candidate_supervisor_queue": candidate_queue,
            "cited_coverage_refs": refs["coverage_refs"],
            "cited_policy_refs": refs["policy_refs"],
            "status_kind": "needs_review",
            "note": "Candidate routing only — the final shift/staffing decision is an authorized "
            "supervisor's; this agent proposes and does not assign.",
            "citation": classified["source"],
        }

    @staticmethod
    def routing_summary(briefs: list[dict[str, Any]]) -> dict[str, Any]:
        """Portfolio-level rollup: exception count, type distribution, exceptions needing urgent review."""
        distribution: dict[str, int] = {}
        for b in briefs:
            distribution[b["exception_type"]] = distribution.get(b["exception_type"], 0) + 1
        urgent = [
            b["exception_id"] for b in briefs if b["exception_type"] == "uncovered_critical_role" or b["is_critical"]
        ]
        return {
            "total_exceptions": len(briefs),
            "type_distribution": distribution,
            "exceptions_needing_urgent_review": urgent,
        }
