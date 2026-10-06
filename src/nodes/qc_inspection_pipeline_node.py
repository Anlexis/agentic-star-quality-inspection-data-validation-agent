"""AgentCore Platform v1.0 — MFG-C2-006 QCInspectionPipelineNode.

The `main` slot of MfgC2006Agent. Runs the four domain steps as a flat,
sequential call chain:

    Parse -> ToleranceValidate -> QCFlag -> GenerateReport

Each step is its own FunctionNode with its own ``execute()`` and audit events.
They are called here through ``execute()`` rather than ``__call__`` because
the caller trust gate already ran at the pre_process slot and these steps read
screened input and derived verdicts only. Any step that returns an error
status short-circuits the rest.

Privacy contract: measurement values live inside each step's ``execute()``;
what travels between the steps through State is derived verdicts.

Audit: emits 'qc_pipeline_complete' or 'qc_pipeline_error'.

Writes: parsed_field_count, parsed_record_count, validation_results,
        tolerance_override_fields, qc_flags, overall_status,
        remediation_suggestions, report_output, status.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.generate_report_node import GenerateReportNode
from src.nodes.parse_node import ParseNode
from src.nodes.qc_flag_node import QCFlagNode
from src.nodes.tolerance_validate_node import ToleranceValidateNode

# Ordered pipeline steps. Instantiated fresh per call — the steps are
# stateless and share no mutable state.
_PIPELINE: tuple[type[FunctionNode], ...] = (
    ParseNode,
    ToleranceValidateNode,
    QCFlagNode,
    GenerateReportNode,
)


class QCInspectionPipelineNode(FunctionNode):
    """Run the inspection workflow behind the `main` backbone slot.

    Trust level: ANONYMOUS — the caller trust gate runs at the pre_process
    slot (PreProcessNode, VERIFIED_EXTERNAL).
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of inspecting a record that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        # Local working copy for step-to-step chaining; the backbone receives
        # the accumulated partial dict, not this copy.
        local_state = dict(state)
        accumulated: dict[str, Any] = {}

        for step in _PIPELINE:
            partial = step().execute(local_state)
            local_state.update(partial)
            accumulated.update(partial)

            # A step stops the pipeline whichever way it declined: a step that
            # completes carrying a reason code produced no verdicts either, so
            # stopping only on ERROR would run the remaining steps on nothing.
            if local_state.get("status") == AgentStatus.ERROR.value or local_state.get("error_code"):
                emit_trace_event(
                    "qc_pipeline_error",
                    {"failed_at": step.__name__},
                    state,
                )
                return accumulated

        flag_count = 0
        try:
            flag_count = len(json.loads(local_state.get("qc_flags", "[]")))
        except (json.JSONDecodeError, TypeError):
            pass

        emit_trace_event(
            "qc_pipeline_complete",
            {
                "field_count": local_state.get("parsed_field_count", 0),
                "record_count": local_state.get("parsed_record_count", 0),
                "overall_status": local_state.get("overall_status", "UNKNOWN"),
                "flag_count": flag_count,
            },
            state,
        )
        return accumulated
