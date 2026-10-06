"""AgentCore Platform v1.0 — MFG-C2-006 QCFlagNode.

Turns the per-dimension verdicts into the quality flag codes a quality
engineer acts on:

  OUT_OF_TOLERANCE              at least one dimension has a reading outside
                                its tolerance band, or an unusable reading;
  MISSING_REQUIRED_MEASUREMENT  a required dimension is absent from the record;
  CAPABILITY_BELOW_THRESHOLD    a dimension's process-capability index sits
                                below the effective threshold;
  METADATA_INCOMPLETE           a mandatory record-identification field is
                                absent from the submission.

Flags are derived from validation_results and the presence of identification
fields — no measurement value is read here.

Audit: emits 'qc_flag_complete'.

Writes: qc_flags (JSON array), status.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.inspection_contract import ContractError, load_specification_rules, parse_measurement_record


class QCFlagNode(FunctionNode):
    """Derive quality flag codes from the per-dimension verdict map.

    Reads ``state["validation_results"]`` and, for the record-identification
    check, the field NAMES present in ``state["input_record"]``.

    Trust level: ANONYMOUS — reads derived verdicts only.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            results: dict[str, dict[str, str]] = json.loads(state.get("validation_results", "{}"))
        except (json.JSONDecodeError, TypeError):
            results = {}

        flags: list[str] = []

        def add(flag: str) -> None:
            if flag not in flags:
                flags.append(flag)

        for verdict in results.values():
            status = verdict.get("status", "")
            if status == "FAIL":
                add("OUT_OF_TOLERANCE")
            elif status == "MISSING":
                add("MISSING_REQUIRED_MEASUREMENT")
            if verdict.get("capability") == "FAIL":
                add("CAPABILITY_BELOW_THRESHOLD")

        # Record-identification check: read the field NAMES the submission
        # carries, never their values.
        try:
            rows = parse_measurement_record(state.get("input_record", ""))
        except ContractError:
            rows = []
        present_fields = {field.lower() for row in rows for field in row}
        required_metadata = [str(name).lower() for name in load_specification_rules().get("iatf_required_fields", [])]
        if any(name not in present_fields for name in required_metadata):
            add("METADATA_INCOMPLETE")

        emit_trace_event(
            "qc_flag_complete",
            {"flags": flags, "flag_count": len(flags)},
            state,
        )
        return {
            "qc_flags": json.dumps(flags),
            "status": AgentStatus.SUCCESS.value,
        }
