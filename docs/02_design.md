# MFG-C2-006 — Design Specification

## What this agent does

A quality engineer receives a measurement record for a produced part — one or more rows of
measured dimensions, plus the identification fields that tie the rows to a part number and a
lot. Deciding whether the lot can be released means checking every dimension against the
tolerance band in the control plan, judging whether the process that produced those readings
is capable, and writing down what has to happen to anything that failed.

MFG-C2-006 does that reduction. It takes a record, evaluates it against the effective
tolerance rules, and returns an inspection report: a disposition, per-dimension verdicts,
quality flags, and remediation guidance. It reports verdicts — never the readings and never
the limits they were judged against.

## Position in the architecture

| Attribute | Value |
|---|---|
| Template ID | MFG-C2-006 |
| Agent class | `MfgC2006Agent` (`src/graph/graph.py`) |
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |
| Category | Cat 2 — a domain pipeline for one specific job |
| Industry | MFG (manufacturing) |
| Generation mode | deterministic — no model is called |
| Required caller trust | VERIFIED_EXTERNAL |

Three-layer separation:

- **State** — a flat `TypedDict` carrying derived inspection data only.
- **Node** — `FunctionNode` subclasses overriding `execute(self, state) -> dict` and returning
  only the keys they changed.
- **Graph** — composition through `register_nodes()`; the standard backbone wiring is used
  unchanged.

## Architecture

### Flat composition

The three open backbone slots hold plain `FunctionNode` classes. The `main` slot holds
`QCInspectionPipelineNode`, which runs the four domain steps as a sequential call chain inside
its own `execute()`. There is no inner graph and no `GraphNode`: the work is a linear
sequence, so a subgraph would add a boundary without adding structure.

One consequence is worth stating, because it differs from the nested shape: the caller's
`input_context` arrives directly in the initial state and every node reads it there. A nested
agent would have to bridge that channel into its inner graph explicitly.

### Backbone

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, up to max_retry)
                                         pre_process
```

| Slot | Node | Responsibility |
|---|---|---|
| initialize | `InitializeNode` (framework) | session id, schema version, caller trust level |
| pre_process | `PreProcessNode` | trust gate, record screening, inspection-profile validation |
| main | `QCInspectionPipelineNode` | parse → tolerance validate → flag → report |
| post_process | `PostProcessNode` | output boundary, formatting |
| finalize | `FinalizeNode` (framework) | response metadata, timings |

### Domain steps inside the main slot

| Step | Node | Reads | Writes |
|---|---|---|---|
| 1 | `ParseNode` | input_record | parsed_field_count, parsed_record_count |
| 2 | `ToleranceValidateNode` | input_record, input_context | validation_results, tolerance_override_fields |
| 3 | `QCFlagNode` | validation_results, input_record | qc_flags |
| 4 | `GenerateReportNode` | validation_results, qc_flags, tolerance_override_fields | overall_status, remediation_suggestions, report_output |

A step that declines stops the chain, whichever way it declined — an error status, or a
completion carrying a reason code. A step that completed carrying a reason produced no verdicts
either, so stopping only on an error status would run the remaining steps on nothing.

### Data flow

```
caller (VERIFIED_EXTERNAL)
  │  measurement record + optional inspection profile
  ▼
PreProcessNode            trust gate; type/size/injection/personal-information screen;
  │                       product_id present; profile validated field by field
  │ input_record, part_family, inspection_stage
  ▼
QCInspectionPipelineNode
  ├─▶ ParseNode                  rows parsed; readings stay local
  ├─▶ ToleranceValidateNode      conformance + capability per dimension
  ├─▶ QCFlagNode                 quality flag codes
  └─▶ GenerateReportNode         disposition, remediation, report body
  ▼
PostProcessNode           screen → redact → screen again → format
  ▼
FinalizeNode
  ▼
{ output: formatted_output, status, trace_id, correlation_id, node_history }
```

## The request contract

Everything a caller may send is defined in `src/nodes/inspection_contract.py`, and every node
that consumes caller data reads it through that one module.

### Measurement record (`input`)

A JSON object, a JSON array of objects, or CSV with a header row. Structural limits, enforced
before anything is evaluated:

| Limit | Value |
|---|---|
| Record size | 200,000 characters |
| Measurement rows | 500 |
| Fields per row | 64 |

### Inspection profile (`input_context`)

| Field | Type | Bound | Default |
|---|---|---|---|
| `part_family` | identifier | `[a-z0-9_]{1,32}` | `""` |
| `inspection_stage` | identifier | `[a-z0-9_]{1,32}` | `final` |
| `tolerance_profile` | map of dimension → `{min, max}` | ≤ 32 entries; each limit finite in [-1e9, 1e9]; `min ≤ max` | `{}` |
| `cpk_threshold` | number | finite in [0, 1000] | the specification's threshold |

Rules that hold for every field:

- **Finite and bounded.** `NaN` and `±Infinity` parse as floats and arrive through raw JSON,
  and every ordered comparison against a `NaN` is False — a `NaN` threshold would silently
  disable the exact decision the agent exists to make. Numerics are therefore parsed by
  `finite_in_range()`, which rejects booleans, non-numerics, non-finite values and
  out-of-range magnitudes, and the request **fails closed** — no inspection is carried out, and
  the run ends as a rejection the caller can correct (see *How a rejected request ends*).
- **Inert.** The two fields rendered into the report are locked to identifiers, so caller text
  can never carry prose — or markup — into the output.
- **Not echoed.** A rejection names the field and never the value.
- **Bounded in count.** The adapter caps the serialized profile at 256 KB and refuses a larger
  one with HTTP 413 before it reaches the graph.
- **Unknown keys are ignored.** Nothing outside the contract influences the pipeline.

The framework's own input gate masks personal information in `user_input` only. This contract
accepts no free text on the profile channel at all, so there is nothing there for a sanitizer
to have to cover.

### Trust boundary on caller-supplied limits

A caller may narrow or widen a tolerance band and may raise or lower the capability
threshold — that is what lets one deployment serve several part families. Two rules keep the
result honest:

1. The **set of required measurements is not caller-controlled**. It is the approved control
   plan; a caller cannot make a missing measurement disappear.
2. Any limit that came from the caller is **disclosed in the report** (`Tolerance source:
   caller-supplied profile (…) merged over the approved specification`), so an inspection
   result can never be read as one evaluated against the approved specification when it was
   not.

## How a rejected request ends

Not every refusal should end the caller's turn, so a rejection ends the run in one of two
shapes. Which shape applies is a property of the refusal itself, not of the node that raised
it, and it is visible in the status the run returns.

### A value the caller can correct — the run completes

The node returns `AgentStatus.SUCCESS` together with an `error_code` naming the category of the
problem. `QCInspectionPipelineNode` passes that marker straight through rather than inspecting a
record that was already declined, and `PostProcessNode` renders the matching sentence from
`src/services/failure_message.py` as the caller-facing body.

| Condition | `error_code` | Sentence the caller reads |
|---|---|---|
| record blank, or whitespace only | `EMPTY_INPUT` | `No question was received. Send the question you want answered.` |
| record longer than 200,000 characters | `QUESTION_TOO_LONG` | `The request is too long. Shorten it and send it again.` |
| record not text; `product_id` absent; an inspection-profile field failed validation; the record does not parse as JSON or CSV | `INVALID_REQUEST` | `A value in the request could not be accepted. Check it against the documented format.` |

Terminating on any of these would close the caller's turn and surface an exception type only,
leaving the reason reachable from the audit trail alone. Completing lets the caller fix the
request and send it again on the same conversation.

**Completing is not succeeding.** No record was inspected, no dimension was evaluated, and no
disposition is reported: the body is the sentence naming what to correct and carries no
`Disposition:` line. The sentence names *what* to correct and nothing else — it never echoes the
rejected value, names an internal field path, or quotes a gate message. Those stay in
`error_log`, the internal audit channel.

### A refusal the agent owns — the run terminates

These are not corrected by rewording. The run ends with `AgentStatus.ERROR`, no
`formatted_output` is written, and the envelope's `output` is empty.

| Condition | Refused by |
|---|---|
| caller below VERIFIED_EXTERNAL | the framework trust gate, before `execute()` runs |
| instruction-override or code-injection content in the record | `PreProcessNode` |
| personal information in the record | `PreProcessNode` |
| credential-shaped or personal-information-shaped content in the generated report | `PostProcessNode`, either screen pass |
| `input_record` absent where the pipeline guarantees it is present | `ParseNode`, `ToleranceValidateNode` |
| the record no longer re-reads, or the inspection profile no longer validates, one step after it did | `ToleranceValidateNode` |

The first four are content the agent refuses to carry whatever the caller sends next; inviting
another attempt would be the wrong answer. The last two are invariant violations: the state that
reached the step contradicts what the step before it guarantees, so evaluating it would produce
a verdict on something nobody checked. Neither kind is reported as a completion.

### `error_code` is not part of the envelope

`get_output()` returns `output`, `status`, `trace_id`, `correlation_id` and `node_history`;
`error_code` is not among them. It is an internal marker — it tells the main slot to stop and
the output boundary which sentence to render — and the reason reaches the caller through that
sentence alone, so nothing the caller has to do depends on holding a code table.

## Evaluation

**Conformance.** A dimension passes when every reading for it sits inside its band. A reading
that cannot be read as a finite number within a plausible magnitude is a FAIL, not a pass — an
unusable reading is not evidence of conformance. A dimension absent from every row is MISSING.

**Capability.** With at least two rows and non-zero spread, the capability index
`min(USL − mean, mean − LSL) / 3σ` is computed per dimension and compared with the effective
threshold. Fewer rows, or zero spread, reports INSUFFICIENT_SAMPLES — never a pass by default.

**Quality flags.**

| Flag | Raised when |
|---|---|
| `OUT_OF_TOLERANCE` | a dimension has a reading outside its band, or an unusable reading |
| `MISSING_REQUIRED_MEASUREMENT` | a required dimension is absent from the record |
| `CAPABILITY_BELOW_THRESHOLD` | a dimension's capability index is below the threshold |
| `METADATA_INCOMPLETE` | a record-identification field is absent from the submission |

**Disposition.** `OUT_OF_TOLERANCE` or `METADATA_INCOMPLETE` → FAIL. Any other flag →
CONDITIONAL (nothing measured was out of band, but the lot cannot be released on this
evidence). No flags → PASS.

## The output boundary

The report states its own schema: **verdicts and dimension names only — no measured value and
no tolerance limit is reproduced**. `PostProcessNode` enforces that independently of the report
builder, so the guarantee does not rest on every future edit to `GenerateReportNode` being
careful.

Two layers run, in this order:

1. **Screen.** The report is scanned for credential-shaped and personal-information-shaped
   content (API keys, JWTs, bearer tokens, credential assignments, national identification
   numbers, payment cards, email addresses). A hit blocks the response: nothing is published
   and the run terminates with an error status — this is a refusal the agent owns, not
   something the caller can reword (see *How a rejected request ends*).
2. **Redact.** Every numeric token in the report is compared with the numbers the request
   actually carried — the submitted readings and the effective tolerance limits, both
   re-derived here because neither is held in State. A token that reproduces one of them
   becomes `[REDACTED]`, with an audit event.

**The order is load-bearing, and the screen runs again after the redaction.** A redaction
rewrites digits, and a structured secret is recognised by the shape of its digits: redacting
first could turn `123-45-6789` into something the screen no longer recognises, and the report
would then ship. Screening first means such content is blocked, never quietly mangled.

**Coverage is by normalisation, not by enumerating forms.** A token is compared by its parsed
magnitude, so `10.05`, `10.050`, `+10.05`, `-10.05`, `10.05mm`, `1.005E+1` and a value written
across a tab or a newline are all the same value to the gate. There is no magnitude exemption:
if the schema says no value is reproduced, that holds for a 0.0001 reading and a six-figure one
alike.

**Names are not readings.** A run of alphanumerics joined by `.`, `-`, `_` or `/` is scanned
unless it is a name. The test is structural: a run counts as a reading when it is built only
from numbers, those separators and an optional short unit suffix — `10.05`, `10.05mm`,
`1.6Ra`, `9.8-10.2`, `1e-05`. Anything else — `SKF-6205`, `LOT-2026-001`, `MFG-C2-006`,
`bearing_6205` — is a name, and the digits inside it are left byte-identical. No list of the
identifier formats a given plant uses is needed. The rule leans towards scanning on purpose:
mistaking a reading for a name would leak it, while mistaking a name for a reading can only
redact a token that happens to equal a submitted value, which fails closed.

A monetary rounding grid, the output invariant used by templates that publish financial
aggregates, does not apply here: this agent renders no monetary values. Its own stated
invariant — no reading, no limit — is what the boundary enforces instead.

## State

| Field | Type | Written by | Notes |
|---|---|---|---|
| user_input | str | caller | the measurement record |
| input_context | dict | caller | the inspection profile |
| input_record | str | PreProcessNode | screened record text |
| part_family | str | PreProcessNode | inert identifier |
| inspection_stage | str | PreProcessNode | inert identifier |
| parsed_field_count | int | ParseNode | derived count |
| parsed_record_count | int | ParseNode | derived count |
| validation_results | str | ToleranceValidateNode | JSON: per-dimension verdicts |
| tolerance_override_fields | str | ToleranceValidateNode | JSON: caller-supplied dimension names |
| qc_flags | str | QCFlagNode | JSON: flag codes |
| overall_status | str | GenerateReportNode | PASS / CONDITIONAL / FAIL |
| remediation_suggestions | str | GenerateReportNode | guidance; no readings |
| report_output | str | GenerateReportNode | report body |
| formatted_output | str | PostProcessNode | the caller-facing result |
| error_code | str | PreProcessNode, ParseNode | reason category on a completed rejection; read by the main slot and the output boundary, never published |
| error_message | str | any node | internal detail recorded alongside a rejection of either shape |
| status | str | any node | the `AgentStatus` value string |

Constraints: a flat `TypedDict` only — no Pydantic models, dataclasses or arbitrary objects,
because checkpoints are serialised with msgpack. No readings, no tolerance limits, no
credentials.

## Security design

| Concern | Implementation |
|---|---|
| Caller authentication | `PreProcessNode.required_trust_level = VERIFIED_EXTERNAL`; the HTTP adapter promotes a caller presenting a valid bearer credential, and leaves everyone else ANONYMOUS |
| Input screening | `PreProcessNode` — type, size, injection and personal-information screens, owned by the node so they hold on a direct `execute()` call too |
| Caller-data validation | `inspection_contract.validate_inspection_profile()` — finite, bounded, inert, fail-closed |
| Output boundary | `PostProcessNode` — screen → redact → screen again |
| Audit | every node calls `emit_trace_event(event, payload, state)` on every code path |

Audit events: `pre_process_complete` / `pre_process_error`, `parse_complete` / `parse_error`,
`tolerance_validate_complete` / `tolerance_validate_error`, `qc_flag_complete`,
`generate_report_complete`, `qc_pipeline_complete` / `qc_pipeline_error`,
`post_process_complete` / `post_process_redacted` / `post_process_degraded` /
`post_process_error`.

## Framework usage

- [x] `InvocationContext` (correlation id, session id, caller trust level)
- [x] `emit_trace_event` on every node path
- [x] `AgentStatus.SUCCESS` / `AgentStatus.ERROR`
- [x] `FunctionNode.execute()` returning a partial state dict
- [x] Runtime parameters from `config/config.yaml` passed as `config=`
- [ ] Model client — this template is deterministic and calls none
- [ ] External services — every input arrives with the request

## Configuration

`config/agent.yaml` is the flat manifest: identity, category, entry class, required trust
level, and the compile-time `requires` block (no secrets, no extras — the pipeline constructs
no client).

`config/config.yaml` holds the runtime parameters (`max_retry`, `timeout_s`). The registry
passes this file to the graph constructor; the standalone server in `src/api/server.py` loads
it and does the same, so a declared value cannot silently fail to reach the graph.

`config/specs.yaml` holds the approved tolerance specification: dimension bands, the required
measurements, the record-identification fields and the capability threshold. Replace it with
the specification for the parts being inspected. Do not commit confidential tolerance values;
load them from a secret store at runtime.

## Import chain

```
src/graph/graph.py                        MfgC2006Agent(AgentBaseGraph)
  src/nodes/pre_process_node.py           PreProcessNode(FunctionNode)
  src/nodes/qc_inspection_pipeline_node.py QCInspectionPipelineNode(FunctionNode)
    src/nodes/parse_node.py               ParseNode(FunctionNode)
    src/nodes/tolerance_validate_node.py  ToleranceValidateNode(FunctionNode)
    src/nodes/qc_flag_node.py             QCFlagNode(FunctionNode)
    src/nodes/generate_report_node.py     GenerateReportNode(FunctionNode)
  src/nodes/post_process_node.py          PostProcessNode(FunctionNode)
  src/nodes/inspection_contract.py        request contract, shared by the nodes above
  src/schemas/state.py                    State(AgentState)
```

Imports come from `framework.*`, `shared.*` and `src.*` only; the platform SDK is never
imported directly.
