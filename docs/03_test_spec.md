# SVC-C2-142 — Test Specification

## Strategy

Three layers, all deterministic (no LLM, no network):

- **unit** — `tests/unit/test_nodes.py`: the deterministic `ShiftCoverageExceptionService` (privacy
  tokenize vs provenance resolution, normalize, classify each policy type + severities, clause retrieval,
  route composition, routing summary) and each node in isolation (S-1/S-2 hygiene + degrade, inner-node
  skip guards with S-4 emit, S-3 fail-closed + disclaimer gate).
- **unit (graph + real invoke)** — `tests/unit/test_graph.py`: outer GraphNode wiring (alias, subgraph
  cache, extract/merge, outer-first error_code), inner-workflow route/registration, and **end-to-end
  through the real `Graph().invoke()`** for grounded / out-of-scope / injection-degrade / oversize-degrade /
  missing-provenance / unsafe-source / PII-tokenised / protected-attribute-dropped / forged-surrogate paths.
- **integration** — `tests/integration/test_end_to_end.py`: multi-exception portfolio prioritisation +
  grounding, mixed cited/uncited fail-closed, empty → out-of-scope.

## Local result (local SDK stub)

- Core suites (`tests/unit/test_graph.py`, `tests/unit/test_nodes.py`, `tests/integration/`): **91 passed,
  1 skipped** (server import skipped when the platform module is unavailable in a local stub env),
  **coverage = 95%** (`--cov=src`, target ≥ 80%).
- Full `tests/` run: **93 passed, 3 skipped, 3 known env-diff failures** (`test_pb_invoke_order`,
  `test_framework_compliance_tc06_tc07::tc06/tc07`). These three assert framework-level `@final`
  enforcement that the local SDK stub shim does not implement; they **pass under the real SDK in CI** and are the
  unchanged scaffold conditional-stub / compliance files (byte-identical to the shipped scaffold and to the
  reference a sibling template).

## Key security test cases (real `Graph().invoke()`)

| # | Case | Expectation |
|---|------|-------------|
| TC-01 | Grounded critical exception (authorized source) | `status=SUCCESS`, `status_kind=supervisor_routing_brief`, `uncovered_critical_role` → `duty_manager_oncall`, cited coverage/policy refs, `human_review.required=True`, DRAFT disclaimer |
| TC-02 | NL text / empty | out-of-scope safe answer, `citations=[]`, disclaimer present |
| TC-03 | Injection payload | degraded `SUCCESS` (never ERROR), post ran (`PostProcessNode` in `node_history`), out-of-scope envelope, marker body absent, terminal S-4 audit carries `error_code=INJECTION_REJECTED` |
| TC-04 | Oversize (> 200 000 chars) | degraded `SUCCESS`, S-4 audit carries `error_code=INPUT_TOO_LONG` |
| TC-05 | Missing provenance | S-3 fail-closed → `needs_review`, brief body withheld, S-4 `error_code=CITATION_INCOMPLETE` |
| TC-06 | Unverifiable / unsafe source (staff name, phone) | `needs_review`, raw source never in output |
| TC-07 | Forged surrogate source (`src:1a2b3c4d` / `exc:deadbeef` / `acct:deadbeef`) | `needs_review`, `citations=[]`, forged value never in output |
| TC-08 | PII / no-space-name `exception_id` (`Alice` / `TaroYamada`) | tokenized `exc:<sha8>`, name never in output, referential integrity across brief ↔ citations |
| TC-09 | Protected-attribute + proxy fields (gender/age/nationality/union/postcode/disability) | dropped pre-LLM — none appear in output |
| TC-10 | Unknown caller field carrying PII | whitelist-by-construction — never reaches output |
| TC-11 | Mixed cited + uncited portfolio | whole grounded brief fails-closed to `needs_review` |

## Reproduce

```bash
source .venv/bin/activate
python -m pytest tests/unit/test_graph.py tests/unit/test_nodes.py tests/integration/ -q --cov=src --cov-report=term
ruff check src tests
python scripts/check_trust_level.py src/
python scripts/check_cat_consistency.py
python scripts/check_dep_pinning.py
```
