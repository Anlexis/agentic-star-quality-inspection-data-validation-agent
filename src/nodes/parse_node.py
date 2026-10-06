"""AgentCore Platform v1.0 — MFG-C2-006 ParseNode.

Turns the screened measurement record (JSON object, JSON array of objects, or
CSV) into a bounded list of measurement rows and publishes the shape of that
submission.

Privacy contract: the parsed values are used inside ``execute()`` only. State
receives the DERIVED shape — how many rows, how many distinct fields — never a
measured value.

Audit: emits 'parse_complete' or 'parse_error'.

Writes: parsed_field_count, parsed_record_count, status, error_message /
        error_log (on rejection).
"""

from __future__ import annotations

from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import INPUT_REJECTED

from src.nodes.inspection_contract import ContractError, parse_measurement_record


class ParseNode(FunctionNode):
    """Parse the screened measurement record into structured rows.

    Reads ``state["input_record"]`` (written by PreProcessNode). Measured
    values stay inside ``execute()``; only derived counts reach State.

    Trust level: ANONYMOUS — parsing reads already-screened input. The caller
    trust gate runs at the pre_process slot.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        input_record = state.get("input_record", "")

        if not input_record:
            emit_trace_event("parse_error", {"reason": "empty_input_record"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ParseNode: input_record is empty"],
                "error_message": "ParseNode: input_record is empty.",
            }

        try:
            # Measurement rows — local to this call, never written to State.
            rows = parse_measurement_record(input_record)
        except ContractError as exc:
            # ContractError carries the category of the problem, not the value.
            emit_trace_event(
                "parse_error",
                {"reason": "parse_failed", "detail": str(exc)},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"ParseNode: {exc}"],
                "error_message": f"Failed to parse measurement record: {exc}.",
            }

        field_names: set[str] = set()
        for row in rows:
            field_names.update(row)

        emit_trace_event(
            "parse_complete",
            {"field_count": len(field_names), "record_count": len(rows)},
            state,
        )
        return {
            "parsed_field_count": len(field_names),
            "parsed_record_count": len(rows),
            "status": AgentStatus.SUCCESS.value,
        }
