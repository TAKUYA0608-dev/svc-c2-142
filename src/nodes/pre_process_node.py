"""SVC-C2-142 — pre_process node: ExceptionRecordIngest + SensitiveDataMinimise (S-1 + S-2, pre-LLM).

Accepts a structured JSON request (an ``exceptions[]`` array of already-reported shift-coverage exception
records, plus optional ``scope`` / ``period``) or NL text, normalizes it (NFKC), enforces S-1/S-2, and
extracts the analysis slots. The agent is read-only: it never mutates the source feed, creates a shift, or
contacts an employee.

Degraded contract (SDK 1.0.0): injection markers / oversize / empty never set ``status=ERROR``. They return
``status=SUCCESS + error_code`` (``INJECTION_REJECTED`` / ``INPUT_TOO_LONG`` / ``INPUT_REJECTED``) and
**discard the offending body** so ``main``/``post_process`` still run (disclaimer + S-3 + S-4). The
``@final`` framework hook is not invoked by the local stub framework, so ``execute()`` re-checks the same S-2
conditions itself. Injection containment is pre-LLM: prompt-like input degrades to a safe out-of-scope
answer *without any semantic execution* of the offending text.

Field-level input hygiene (S-1) + sensitive-data minimisation (S-2, pre-LLM, labour-data):
- every string written into ``validated_input`` is passed through ``_hygiene()`` (redacts credential /
  My-Number / email / phone patterns);
- employee **PII display fields** (name / contact) are DROPPED — never masked;
- **protected-attribute and known-proxy fields** (gender / age / nationality / religion / disability /
  union membership / marital status / pregnancy / medical, and proxies like postcode / childcare) are
  DROPPED entirely before the workflow runs — they are never used, inferred, or carried;
- identifier fields (``exception_id`` / ``staff_ref`` / ``site_ref``) are UNCONDITIONALLY tokenized to an
  opaque, non-reversible surrogate (a bare name is opaque like any value), with no surrogate→raw rejoin
  map kept in state;
- caller-supplied provenance (``source``) is constrained to a grounded citation only if it resolves to an
  authorized workforce system of record; anything else is dropped so untrusted text can never reach a
  citation. ``scope`` / ``period`` are hygiened too.
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.services.service import opaque_id, resolve_provenance
from src.utils.audit import emit_trace_event

_MAX_INPUT = 200_000  # coverage payloads can carry many exception records → larger cap than a chat prompt
_INJECTION_MARKERS = (
    "ignore previous",
    "ignore all previous",
    "disregard the above",
    "system prompt",
    "you are now",
    "###system",
    "<|im_start|>",
)
_REJECT_CODES = frozenset({"INJECTION_REJECTED", "INPUT_TOO_LONG"})
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# ── input hygiene: redact secrets a caller may inadvertently include before persisting to State ──
_CREDENTIAL = re.compile(r"\b(sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,})\b")
_MY_NUMBER = re.compile(r"\b\d{12}\b")  # Japanese My-Number / 個人番号
_EMAIL = re.compile(r"\b[\w.+-]{1,64}@[\w-]{1,63}(?:\.[\w-]{1,63}){1,4}\b")
_PHONE = re.compile(r"(?<![\d.])(?:(?:\+81[-\s]?\d{1,4}|0\d{1,4})[-\s]?\d{1,4}[-\s]?\d{3,4})(?![\d.])")
_REDACTED = "[REDACTED]"

# PII display fields dropped entirely — exceptions are keyed by opaque IDs, never by name/contact.
_PII_DROP_FIELDS = frozenset(
    {
        "staff_name",
        "employee_name",
        "worker_name",
        "name",
        "full_name",
        "contact_name",
        "contact_email",
        "contact_phone",
        "email",
        "phone",
        "address",
        "home_address",
    }
)
# Protected-attribute + known-proxy fields DROPPED before the workflow (S-2, labour-data): never used,
# inferred, or carried. Dropping (not masking) removes any downstream discrimination/bias surface.
_PROTECTED_DROP_FIELDS = frozenset(
    {
        "gender",
        "sex",
        "age",
        "birthdate",
        "dob",
        "date_of_birth",
        "nationality",
        "ethnicity",
        "race",
        "religion",
        "disability",
        "disabled",
        "marital_status",
        "married",
        "pregnancy",
        "pregnant",
        "sexual_orientation",
        "union",
        "union_member",
        "union_membership",
        "medical",
        "medical_condition",
        "health",
        "health_condition",
        "childcare",
        "has_children",
        "dependents",
        "postcode",
        "zipcode",
    }
)
# Identifier keys are unconditionally tokenized to an opaque surrogate — no syntactic passthrough — so a
# PII / free-text identifier can never leak into State, a citation, or output. prefix chosen per key.
_ID_PREFIX = {
    "exception_id": "exc",
    "id": "exc",
    "staff_ref": "staff",
    "staff_id": "staff",
    "site_ref": "site",
    "site_id": "site",
}


def _nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def _hygiene(text: str) -> str:
    """Redact credential / My-Number / email / phone patterns from a free-text value."""
    out = _CREDENTIAL.sub(_REDACTED, text)
    out = _MY_NUMBER.sub(_REDACTED, out)
    out = _EMAIL.sub(_REDACTED, out)
    out = _PHONE.sub(_REDACTED, out)
    return out


def _hygiene_obj(obj: Any) -> Any:
    """Recursively drop PII / protected-attribute fields, tokenize identifiers, constrain provenance to an
    authorized citation, and redact secrets in every string value."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            key = k.lower()
            if key in _PII_DROP_FIELDS or key in _PROTECTED_DROP_FIELDS:
                continue  # dropped, not masked
            if key in _ID_PREFIX:
                text = str(v).strip() if v is not None else ""
                out[k] = opaque_id(text, _ID_PREFIX[key]) if text else None
                continue
            if key == "source":
                # Provenance → grounded citation only if it resolves to an authorized system of record
                # (privacy-tokenized); unverifiable / free-text source → None → S-3 blocks (needs_review).
                out[k] = resolve_provenance(v)
                continue
            out[k] = _hygiene_obj(v)
        return out
    if isinstance(obj, list):
        return [_hygiene_obj(v) for v in obj]
    if isinstance(obj, str):
        return _hygiene(obj)
    return obj


class PreProcessNode(FunctionNode):
    """Validate + minimise the exception request and extract its exceptions / scope / period slots."""

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _reject_code(self, raw: str) -> str | None:
        if len(raw) > _MAX_INPUT:
            return "INPUT_TOO_LONG"
        if any(marker in _nfkc(raw).lower() for marker in _INJECTION_MARKERS):
            return "INJECTION_REJECTED"
        return None

    def _extra_security_gate_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """S-2 domain checks: size cap + prompt-injection markers (pre-LLM containment).

        SDK 1.0.0 contract: MUST NOT raise, and MUST NOT set status=ERROR (that would short-circuit the
        pipeline past post_process). A rejection is surfaced as a degraded ``SUCCESS + error_code``; the
        offending body is discarded by execute() with no semantic execution of the prompt-like text.
        """
        raw = state.get("user_input", "") or ""
        code = self._reject_code(raw)
        if code:
            out = dict(state)
            out["error_code"] = code
            return out
        return dict(state)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("user_input", "") or ""
        input_context = state.get("input_context", {})  # read-only caller context [C1]
        enriched = json.dumps(
            {"source": "ServiceShiftCoverageExceptionRouterAgent", "channel": input_context.get("channel", "unknown")},
            ensure_ascii=False,
        )

        # Degrade on rejection: the S-2 hook may already have set error_code (real SDK); re-detect here
        # because the local stub framework does not invoke the hook. Discard the offending body entirely.
        prior = state.get("error_code")
        code = prior if prior in _REJECT_CODES else self._reject_code(raw)
        if code:
            emit_trace_event("exception_record_ingest.rejected", {"reason": code}, state)
            return {
                "validated_input": "{}",
                "input_format": "rejected",
                "enriched_context": enriched,
                "user_input": "",
                "error_code": code,
                "status": AgentStatus.SUCCESS.value,
            }

        if not raw.strip():
            emit_trace_event("exception_record_ingest.rejected", {"reason": "empty_input"}, state)
            return {
                "validated_input": "{}",
                "input_format": "empty",
                "enriched_context": enriched,
                "error_code": "INPUT_REJECTED",
                "status": AgentStatus.SUCCESS.value,
            }

        slots, fmt = self._parse(_CONTROL.sub("", _nfkc(raw)))
        emit_trace_event(
            "exception_record_ingest.validated",
            {"input_format": fmt, "exception_count": len(slots["exceptions"]), "scope": slots.get("scope")},
            state,
        )
        return {
            "validated_input": json.dumps(slots, ensure_ascii=False),
            "input_format": fmt,
            "enriched_context": enriched,
            "status": AgentStatus.SUCCESS.value,
        }

    def _parse(self, text: str) -> tuple[dict[str, Any], str]:
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return {"exceptions": [], "scope": None, "period": None}, "text"
        if isinstance(obj, dict):
            exceptions = obj.get("exceptions")
            exceptions = exceptions if isinstance(exceptions, list) else []
            # scope / period are untrusted free text → hygiene them too (S-3 re-redacts at output).
            return {
                "exceptions": _hygiene_obj(exceptions),
                "scope": _hygiene_obj(obj.get("scope")),
                "period": _hygiene_obj(obj.get("period")),
            }, "json"
        if isinstance(obj, list):  # bare exceptions array
            return {"exceptions": _hygiene_obj(obj), "scope": None, "period": None}, "json"
        return {"exceptions": [], "scope": None, "period": None}, "text"
