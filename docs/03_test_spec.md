# MFG-C2-006 — Test Specification

## Overview

| Attribute | Value |
|---|---|
| Template ID | MFG-C2-006 |
| Agent class | `MfgC2006Agent` (`AgentBaseGraph`, flat Cat 2) |
| Backbone | initialize → pre_process → main (`QCInspectionPipelineNode`) → post_process → finalize |
| Test framework | pytest |
| Framework version under test | `agenticstar-agentcore[anthropic]==1.0.1` |

## Strategy

- Every security-relevant path is covered: the caller trust gate, the input screens, the
  request contract, and the output boundary.
- Three levels: contract-level unit tests, node-level unit tests, and boundary tests that go
  through a compiled graph or the real ASGI entry point.
- The privacy contract is a test constraint as well as a product one: no test asserts on a
  measured value appearing in an output, because none ever should.
- Input-screening tests call `execute()` **directly**, with no framework wrapper in front of
  the node. A refusal that only holds while the framework's input gate is active is not a
  guarantee this template owns. Trust-gate tests do the opposite and go through `__call__`,
  which is where that gate lives.
- A rejection is asserted by its **shape**, not merely by the absence of a report. A value the
  caller can correct completes with `AgentStatus.SUCCESS`, carries a reason code, and renders a
  correction sentence with no `Disposition:` line; a refusal the agent owns — injection content,
  personal information, a blocked report, a broken upstream invariant — terminates with
  `AgentStatus.ERROR` and publishes nothing. The tests assert which of the two happened, so a
  change that silently turned one into the other would fail. The two shapes are specified in
  `docs/02_design.md` → *How a rejected request ends*.

## Suite structure

```
tests/
├── proof_of_boundary/
│   ├── test_pb_invoke_order.py               backbone order + the output boundary
│   ├── test_pb_invoke_endpoint.py            end-to-end through the ASGI /invoke entry
│   ├── test_pb7_hitl_interrupt_propagation.py interrupt propagation (skipped: not enabled)
│   ├── test_import_isolation.py              no direct platform-SDK import
│   └── test_state_safety.py                  State carries no credential-shaped field
└── unit/
    ├── test_inspection_contract.py           the request contract
    ├── test_nodes.py                         the domain nodes
    ├── test_output_gate.py                   the output boundary
    └── test_framework_compliance_tc06_tc07.py the framework gates are final
```

## Reference payload

```
{"product_id": "PART-QC-001", "lot_number": "LOT-2026-001",
 "length": 10.0, "width": 5.0, "surface_roughness": 0.8}
```

Every reading is inside the specification bands (length [9.8, 10.2], width [4.9, 5.1],
surface roughness [0.0, 1.6]), `product_id` is present, and the record carries no personal
information and no injection content. `deploy/invoke_payload.json` carries the same record.

## Boundary tests

### `test_pb_invoke_order.py` — backbone order and the output boundary

| Case | Expected |
|---|---|
| Authenticated external caller invokes with the reference payload | SUCCESS; node_history = InitializeNode → PreProcessNode → QCInspectionPipelineNode → PostProcessNode → FinalizeNode, in order |
| `result["output"]` on the success path | a non-empty string |
| `result["status"]` | exactly `str` — the `AgentStatus` value, never a bare enum |
| Clean report through `PostProcessNode` | SUCCESS; formatted_output set |
| Report carrying an API key | ERROR; no formatted_output |
| Report reproducing a submitted reading | SUCCESS; the reading is redacted from the output |

The caller context is VERIFIED_EXTERNAL, never an internal one: an internal context outranks
every inner node's ANONYMOUS requirement, so the gate would always pass and the path a
production caller takes would never be exercised. The success path is required for the order
assertion — a non-success status short-circuits main → post_process.

### `test_pb_invoke_endpoint.py` — end-to-end through `/invoke`

| Case | Expected |
|---|---|
| `GET /health` | 200, `{"status": "ok"}` |
| Runtime config | `max_retry` and `timeout_s` from `config/config.yaml` reached the compiled graph |
| Authenticated submission with a profile | SUCCESS; report computed from these readings and this profile |
| Same record, tightened caller band | the disposition flips to FAIL and the report discloses the caller-supplied source |
| A different record | a different report |
| Record missing required measurements | CONDITIONAL |
| No credential / wrong credential | ERROR; nothing published |
| Invalid profile (10 forms incl. `NaN`, `Infinity`, out-of-range, non-identifier, inverted band, unknown sub-key, over-cap map) | SUCCESS carrying the correction sentence; no `Disposition:` reported; the value never appears in the response |
| Bare `NaN` / `Infinity` / `-Infinity` literals in the raw request body | SUCCESS carrying the correction sentence and no `Disposition:`, or a 400/422 from the adapter — never a report |
| `input_context` over 256 KB | HTTP 413 at the adapter |
| Record over the size limit | SUCCESS carrying the size sentence; no `Disposition:` reported |
| Injection content in the record | ERROR; nothing published |
| Personal information in the record | ERROR; nothing published |
| The returned report | reproduces no submitted reading and no effective tolerance limit |
| Identifiers in the report | byte-identical; no redaction marker present |

### `test_pb7_hitl_interrupt_propagation.py`

Skipped: this template enables no cross-boundary interrupt propagation, so there is no
propagation behaviour to assert. The module is real and importable, and the assertion is
written the day an interrupt checkpoint is wired.

### `test_import_isolation.py` / `test_state_safety.py`

`src/` imports nothing from the platform SDK directly; the State definition declares no
credential-shaped field and no prohibited type.

## Unit tests

### `test_inspection_contract.py` — the request contract

| Group | Cases |
|---|---|
| `finite_in_range` | `NaN`, `+Infinity`, `-Infinity`, booleans, non-numerics, out-of-range → rejected; in-range values → accepted with the parsed magnitude |
| Record parsing | JSON object, JSON array, CSV; malformed text, non-text input, oversized record, too many rows, too many fields → refused; the row limit itself accepted |
| Profile defaults | absent or `None` profile → documented defaults |
| Rendered strings | `part_family`, `inspection_stage` reject spaces, uppercase, over-length, empty, non-strings |
| `cpk_threshold` | non-finite matrix, boolean, string, out-of-range → refused |
| Tolerance limits | non-finite matrix per bound, inverted band, missing bound, unknown sub-key, non-identifier unit, non-identifier dimension name, over-cap map → refused |
| Rejection hygiene | the rejected value never appears in the returned profile; unknown keys are ignored |
| Effective rules | specification used when no profile; caller limits replace the band and are disclosed; a caller may add a dimension; the required-measurement set is not caller-controlled |
| Value collection | non-numeric fields ignored; non-finite readings skipped; limits and threshold collected |

### `test_nodes.py` — the domain nodes

| Node | Cases |
|---|---|
| `PreProcessNode` | valid record accepted; empty / mistyped / oversized records and a record missing `product_id` complete carrying a reason code; seven injection forms terminate with no `input_record` written; four benign records containing the same words accepted; four personal-information forms terminate; valid profile carried into state; eight invalid-profile forms complete carrying a reason code, name the failing field in `error_message` and write no `input_record`; the rejected value is never echoed |
| Trust gate | ANONYMOUS caller denied through `__call__`; VERIFIED_EXTERNAL admitted; every node declares its trust level |
| `ParseNode` | JSON and CSV shapes; an absent `input_record` terminates; a malformed record completes carrying a reason code; readings never reach the returned state |
| `ToleranceValidateNode` | conforming PASS; out-of-band FAIL; absent MISSING; `NaN`/`Infinity`/non-numeric readings FAIL closed; a caller band flips the verdict and is disclosed; capability PASS / FAIL / INSUFFICIENT_SAMPLES; a caller threshold flips the capability verdict; an empty record and a profile that no longer validates terminate; readings never reach the returned state |
| `QCFlagNode` | each of the four flags raised on its trigger; no flags when everything conforms; malformed verdict JSON does not raise |
| `GenerateReportNode` | each disposition; remediation for a failing dimension; tolerance source disclosed both ways; profile identifiers rendered; the schema note present; malformed input does not raise |
| `QCInspectionPipelineNode` | the full chain produces a report; a step that declines stops the chain whichever way it declined; a request declined upstream is passed through carrying its reason code, never re-inspected |

### `test_output_gate.py` — the output boundary

| Group | Cases |
|---|---|
| Screen | eight blocked content classes detected; five ordinary report lines pass |
| Redaction coverage | fourteen representations of one reading — plain, trailing zeros, signed both ways, unit-attached, two exponent forms, tab, newline, parentheses, brackets, `=`-attached, and both ends of a range — all redacted; tolerance limits redacted too; every occurrence redacted; no magnitude exemption (0.0001 and 987654 alike) |
| Names | nine identifier forms byte-identical while their magnitudes are in the value set; a standalone number is scanned wherever it sits, including as a section number; a number with a unit suffix is still a reading |
| Layer order | three structured secrets whose every digit group is also a submitted reading are BLOCKED, not mangled; the post-redaction screen catches what only becomes secret-shaped after digits move |
| Node | clean report passes; a leaked reading is redacted; a caller-supplied limit is redacted; the declared state key is written; the trust level is declared |
| `request_values` | readings and effective limits collected; an unreadable record yields the limits only; an invalid profile contributes nothing |

### `test_framework_compliance_tc06_tc07.py`

| Case | Expected |
|---|---|
| TC-06 — a subclass overriding the default input gate | `TypeError` at class definition |
| TC-07 — a subclass overriding the default output gate | `TypeError` at class definition |

## Coverage of the security-relevant paths

| Requirement | Where it is proven |
|---|---|
| Caller trust gate | `TestTrustGate`; unauthenticated and wrong-credential cases in the endpoint suite |
| Injection refused, owned by the node | `TestPreProcessNodeScreening` (direct `execute()`), both directions; endpoint suite end-to-end |
| Personal information refused | `TestPreProcessNodeScreening`; endpoint suite |
| Caller numerics finite, bounded, fail-closed | `TestFiniteInRange`, `TestValidateInspectionProfile`, `TestPreProcessNodeProfile`, endpoint non-finite matrix |
| Caller strings inert | identifier cases in the contract and node suites |
| Structural limits | record/row/field caps in the contract suite; 256 KB cap in the endpoint suite |
| Rejected values never echoed | contract, node and endpoint suites |
| A correctable rejection completes; a refusal the agent owns terminates | `TestPreProcessNodeScreening`, `TestPreProcessNodeProfile`, `TestQCInspectionPipelineNode`; the endpoint suite's invalid-profile, oversized-record, injection and personal-information cases |
| Output boundary blocks what must not publish | `TestBlockedContentScreen`, `TestLayerOrder`, boundary suite |
| The report reproduces no reading and no limit | `TestRedactionCoversEveryRepresentation`, `test_report_reproduces_no_reading_and_no_limit` |
| Identifiers unharmed | `TestNamesAreNotReadings`, `test_identifiers_in_the_report_are_untouched` |

## Running the tests

```bash
python -m pytest tests/ -v
python -m pytest tests/proof_of_boundary/ -v
python -m pytest tests/unit/ -v
```

The suite runs without a platform connection. Running the agent itself does not.
