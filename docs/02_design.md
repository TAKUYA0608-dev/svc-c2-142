# Template Design Specification — SVC-C2-142

Service Shift-Coverage Exception Router (Cat 2, GraphNode-in-main).

## Position in AgentCore Architecture

- **Agent Class**: `ServiceShiftCoverageExceptionRouterAgent` (module-level alias of `Graph`)
- **L1 Base**: AgentBaseGraph (L1 direct — Cat 2 GraphNode-in-main; **not** AutonomousBaseGraph). The
  `DocGenerationAgent` L2 pattern is a design reference only; the workflow is implemented directly on
  AgentBaseGraph (2026-05-18 L2-deprecation ruling).
- **Category**: Cat 2 — orchestrates a fixed multi-step workflow to produce one job-to-be-done deliverable
  (a SupervisorRoutingBrief for an already-reported shift-coverage exception).
- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible); complex fields are JSON strings (ADR-005)
  - Node: L1 inheritance (Template Method: `execute(self, state: dict) -> dict` override only — no `config` param)
  - Graph: composition (`register_nodes()` for node substitution; domain complexity behind a `GraphNode`)
- **LLM**: none. The template is **fully deterministic** (threshold banding + set membership + keyed clause
  composition against a seeded approved coverage matrix / escalation policy). There is no model in
  `config/agent.yaml`, no LLM dependency in `pyproject.toml`, and no LLM call anywhere in `src/`. "pre-LLM"
  in the S-2 discussion below therefore means "before any downstream node reads the free-text context".

## Architecture Overview

### Node Configuration (outer 5-slot backbone)

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | schema/session/trust setup | user_input | caller_trust_level, session_id | InitializeNode (default) |
| pre_process | `ExceptionRecordIngest` + `SensitiveDataMinimise` — S-1 normalisation (NFKC, size cap) + S-2 pre-LLM sensitive-data minimisation. **injection/oversize → degraded `SUCCESS + error_code`, offending body discarded (never `status=ERROR`)**. Field-level input hygiene (credential/My-Number/email/phone redaction); **PII display fields dropped**; **protected-attribute + known-proxy fields DROPPED (not masked)** before the workflow; identifiers (`exception_id`/`staff_ref`/`site_ref`) **UNCONDITIONALLY tokenized to opaque, non-reversible surrogates** (a bare name is opaque like any value, no surrogate→raw rejoin map kept); **provenance `source` resolved to a citation ONLY if it names an authorized workforce system of record (privacy-tokenized `src:<sha8>`), else dropped to `None`** — S-3 then blocks; `scope`/`period` hygiened | user_input | validated_input, input_format, enriched_context, (error_code) | PreProcessNode (FunctionNode) |
| main | `ShiftCoverageExceptionRouteWorkflowGraphNode` — wraps inner `ShiftCoverageExceptionRouteWorkflow` (composition criterion #9) | validated_input | result, classified_count, human_review_required, (error_code), status | GraphNode (subgraph) |
| post_process | `OutputSanitise` — S-3 output gate: **fail-closed per-exception citation completeness** (ungrounded brief → `needs_review` degrade, brief body withheld, `error_code=CITATION_INCOMPLETE`) + staff-name/company/phone/email/credential/My-Number re-redaction + DRAFT disclaimer, S-4 no-persist audit | result | formatted_output, disclaimer, audit_logged, (error_code) | PostProcessNode (FunctionNode) |
| finalize | build response envelope | formatted_output | output, status | FinalizeNode (default) |

### Inner workflow (`src/graph/domain_workflow_graph.py` — BaseGraph, linear + per-node skip guard)

```
START → exception_classify → coverage_reference_retrieve → supervisor_route_compose → human_gate → END
```

| Inner Node | Responsibility | Skip guard |
|------|---------------|-----------|
| exception_classify | Deterministic ingest + normalize of the supplied already-reported exception records against the seeded approved coverage matrix; compute derived signals (coverage gap, missing skills, notice lateness, demand-capacity gap, criticality); classify each into a policy-defined type (uncovered_critical_role / skill_mismatch / late_absence / demand_capacity_conflict / unclassified) with matched drivers + evidence; set `classified_count`. **0 valid exceptions → `error_code=NO_EXCEPTIONS` → out-of-scope safe answer**. Free-text context is never interpreted semantically | — (first node; emits `.skip` on rejected/no-exception input) |
| coverage_reference_retrieve | Deterministic retrieval of the cited coverage-matrix + escalation-policy clauses (`<clause_id>@<version>`) for each classified exception, grounding the routing brief in authorized clauses | no-op `return {}` (after `.skip` emit) on `error_code` / `classified_count == 0` |
| supervisor_route_compose | Compose the SupervisorRoutingBrief deliverable: per exception, the exception type, a candidate supervisor queue (escalation tier + SLA), cited coverage/policy clauses, a needs-review mark, and the source citation; ordered by priority (critical/urgent first). On 0-exception/rejected → out-of-scope safe answer | emits safe answer on `error_code` / no exceptions |
| human_gate | Deterministic **HumanApprovalGate**: mark `human_review_required=True` + `review_status="pending_human_approval"`, record material decisions (critical/urgent exceptions and any candidate routing requiring an authorized supervisor's sign-off before a shift/staffing action) into the brief. Supervisor assignment is **never** made by the agent | no-op `return {}` (after `.skip` emit) on `error_code` / `classified_count == 0` (safe answer needs no human gate) |

`ShiftCoverageExceptionRouteWorkflowGraphNode.get_subgraph()` caches the compiled inner workflow on the **class attribute**
(`ShiftCoverageExceptionRouteWorkflowGraphNode._subgraph`, not `self` — avoids mutable node-instance state per §9; built once; `BaseGraph.invoke()` `_ensure_compiled` is idempotent). `extract_input()`
passes `validated_input` into the inner graph; `merge_output()` surfaces `result / classified_count /
human_review_required / error_code / status` — with **`error_code` OUTER-first** (`state.get("error_code")
or sub_result.get("error_code")`) so a pre-stage rejection survives to the terminal S-4 audit (the inner
workflow runs on the discarded body and would otherwise overwrite it with `NO_EXCEPTIONS`).

## Security Model (S-1 … S-5)

- **S-1 (input normalisation + field hygiene)**: NFKC + control-char strip + size cap; every string written
  into `validated_input` is passed through credential/My-Number/email/phone redaction; identifiers are
  tokenized to opaque surrogates; provenance resolved exactly once here.
- **S-2 (pre-LLM sensitive-data minimisation, labour-data)**: protected-attribute and known-proxy fields
  (gender/age/nationality/religion/disability/union membership/marital status/pregnancy/medical, and proxies
  such as postcode/childcare) are **dropped entirely — not masked** — before the workflow runs, so they are
  never used, inferred, or carried. Opaque IDs are non-reversible one-way hashes with **no surrogate→raw
  rejoin map in graph state**; the S-4 audit references only minimised counts. **Injection containment is
  pre-LLM**: prompt-injection markers or oversize input degrade to a safe out-of-scope answer *without any
  semantic execution of the offending text*.
  - **Degraded contract (SDK 1.0.0)**: an S-2 rejection is surfaced as **`status=SUCCESS` + `error_code`**
    (`INJECTION_REJECTED` / `INPUT_TOO_LONG` / `INPUT_REJECTED`) with the offending body discarded — it is
    **never `status=ERROR`** (which would short-circuit `route()` straight to `finalize`, skipping
    `post_process` and thus the disclaimer / S-3 redaction / S-4 audit). `post_process` therefore always
    runs and always delivers the out-of-scope safe answer + disclaimer + audit. `_extra_security_gate_input`
    MUST NOT raise and MUST `return dict(state)`; `execute()` re-checks the same conditions because the
    local stub framework does not invoke the `@final` hook.
- **S-3 (output gate, fail-closed)**: enforce per-exception citation completeness — a grounded
  SupervisorRoutingBrief in which any exception lacks a verifiable `source` citation is **never presented**;
  it degrades to `needs_review` with the brief body withheld (`error_code=CITATION_INCOMPLETE`, still
  SUCCESS). Re-redact any leaked secret/contact/name pattern (defense-in-depth). Append the mandatory DRAFT
  advisory disclaimer. `_extra_security_gate_output` receives the `execute()` result delta and MAY raise to
  block an output missing the disclaimer.
- **S-4 (audit, no-persist)**: every node `execute()` path — including every skip/0-count/degraded branch —
  emits a count-only domain trace event via `src.utils.audit.emit_trace_event`; payloads carry counts /
  type distribution / policy-type keys / error codes only (no staff name, free-text context, or protected
  attribute). The raw record is not retained.
- **S-5 (rate limit / abuse)**: enforced at the platform entry point; the agent is read-only and performs
  no external write.

## Read-only / non-execution boundary

The agent **never** creates or changes a shift, contacts an employee, infers availability or a protected
attribute, approves overtime, or makes a staffing decision. All output is **candidate / needs-review**, and
the final shift/staffing decision is always an authorized human supervisor's, gated by the HumanApprovalGate
(the candidate supervisor queue is a policy-defined role queue, never a named person).

## Open Items (Stage ③ implementation plan)

The design MR ships `docs/02` + `src/schemas/state.py` only. The Stage ③ implementation MR adds: the six
node implementations (pre_process, the four inner nodes, post_process), the inner/outer graph wiring
(`get_subgraph` caching + `merge_output` outer-first error_code), the deterministic
`ShiftCoverageExceptionService` (seeded coverage matrix + escalation policy + provenance/opaque-id helpers),
`src/utils/audit.py` (S-4 shim), the `ServiceShiftCoverageExceptionRouterAgent = Graph` registry alias, and
the unit / integration / real-invoke tests (including the forged-surrogate and protected-attribute
regressions). Seeded coverage matrix / escalation policy are CoE-calibratable via a change-controlled
engineer MR + specialist review — they are not runtime-editable operational actions.
