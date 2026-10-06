"""AgentCore Platform v1.0 — MFG-C2-006 State schema.

State is a flat ``TypedDict`` extending ``AgentState`` — never a Pydantic
model: checkpoints are serialised with msgpack, and rich objects do not
survive that round trip intact. Domain fields are ``NotRequired`` so the
TypedDict is valid at graph initialisation, before any node has written.

Privacy contract: a measurement record carries proprietary process data.

  * Measured values and tolerance limits are used inside individual node
    ``execute()`` calls and are never written here.
  * State holds DERIVED information only: counts, per-dimension verdicts,
    quality flag codes, remediation guidance and the rendered report.
  * No credentials, tokens or personal information, ever.
"""

# The framework package ships without a py.typed marker, so AgentState resolves
# to Any for a type checker and this class is not recognised as a TypedDict —
# which makes NotRequired[] unverifiable HERE. At runtime AgentState is a
# genuine TypedDict and these fields must stay NotRequired (nodes write them;
# they are absent at graph initialisation), so the annotations stay and only
# that one check is disabled.
# mypy: disable-error-code="valid-type"

from typing import NotRequired, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """MFG-C2-006 agent state — derived inspection data only."""

    # Screened measurement record (JSON or CSV text) written by PreProcessNode.
    # Carries the caller's own submission; no specification data is added to it.
    input_record: NotRequired[str]

    # Inert identifiers taken from the validated inspection profile. Both are
    # rendered into the report, so both are constrained to [a-z0-9_]{1,32}.
    part_family: NotRequired[str]
    inspection_stage: NotRequired[str]

    # Shape of the submission — DERIVED counts, not readings.
    parsed_field_count: NotRequired[int]
    parsed_record_count: NotRequired[int]

    # Per-dimension verdicts, JSON object:
    #   {"<dimension>": {"status": "PASS"|"FAIL"|"MISSING",
    #                    "detail": "<category>",
    #                    "capability": "PASS"|"FAIL"|"INSUFFICIENT_SAMPLES"}}
    validation_results: NotRequired[str]

    # Dimension names whose limits came from the caller profile rather than
    # from the approved specification — JSON array of identifiers.
    tolerance_override_fields: NotRequired[str]

    # Quality flag codes — JSON array of:
    #   OUT_OF_TOLERANCE | MISSING_REQUIRED_MEASUREMENT
    #   | CAPABILITY_BELOW_THRESHOLD | METADATA_INCOMPLETE
    qc_flags: NotRequired[str]

    # Aggregate disposition: "PASS" | "FAIL" | "CONDITIONAL".
    overall_status: NotRequired[str]

    # Remediation guidance for anything that did not pass. Carries dimension
    # names and actionable text — no readings, no limits.
    remediation_suggestions: NotRequired[str]

    # Rendered report body, written by GenerateReportNode and gated by
    # PostProcessNode.
    report_output: NotRequired[str]

    # Error detail set alongside an error status.
    error_message: NotRequired[str]
    error_code: Optional[str]
