"""AgentCore Platform v1.0 — MFG-C2-006 PreProcessNode.

The input boundary of the inspection pipeline. Two responsibilities:

  1. Measurement-record screening — type check, empty check, size guard,
     injection scan, personal-information detection (or the ``[MASKED]``
     sentinel the framework input gate substitutes for detected personal
     information), and the presence of the mandatory ``product_id`` field.
  2. Inspection-profile validation — every field the caller sends on the
     ``input_context`` channel is validated against explicit bounds before any
     later node may consume it. Invalid values fail CLOSED: the request is
     rejected naming the field, never echoing the value. Absent fields take
     their documented defaults.

The node owns these refusals itself rather than relying on the framework's
input gate: the guarantee must hold wherever the node runs, including a direct
``execute()`` call with no framework wrapper in front of it.

Every code path emits an audit event.

Writes: input_record (screened), part_family, inspection_stage, status,
        error_message / error_log (on rejection).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress

from src.nodes.inspection_contract import MAX_RECORD_CHARS, validate_inspection_profile

# Instruction-override and code-injection attempts. A measurement record is
# structured data, so none of these forms has a legitimate reading here.
_INJECTION_RE = re.compile(
    r"(?:ignore\s+(?:all\s+)?previous\s+instructions"
    r"|disregard\s+(?:your|the)\s+(?:prompt|instructions)"
    r"|you\s+are\s+now\s+"
    r"|system\s+prompt"
    r"|<\s*script"
    r"|javascript\s*:"
    r"|;\s*DROP\s+TABLE"
    r"|UNION\s+SELECT"
    r"|\bOR\s+1\s*=\s*1"
    r"|\|\|\s*'"
    r"|\b(?:exec|eval)\s*\()",
    re.IGNORECASE,
)

# Personal information has no place in a measurement record. The framework
# input gate masks what it detects to [MASKED] before execute() runs; the raw
# patterns catch the same content on a direct execute() call.
_PII_RE = re.compile(
    r"(?:\b\d{3}-\d{2}-\d{4}\b"  # national identification number
    r"|\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"  # payment card
    r"|\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"  # email address
    r"|\[MASKED\])"
)


def _reject(reason: str, message: str, state: dict[str, Any], code: str = "INVALID_REQUEST") -> dict[str, Any]:
    """Build a fail-closed rejection and audit it.

    The reason names the failing category or field; the rejected value is
    never carried into the audit payload, the error log, or the response.
    """
    emit_trace_event("pre_process_error", {"reason": reason}, state)
    if code:
        # A value the caller can correct: the run COMPLETES carrying the
        # reason so the request can be sent again on the same conversation.
        emit_progress(INPUT_REJECTED)
        return {
            "status": AgentStatus.SUCCESS.value,
            "error_code": code,
            "error_log": [f"PreProcessNode: {reason}"],
            "error_message": message,
        }
    return {
        "status": AgentStatus.ERROR.value,
        "error_log": [f"PreProcessNode: {reason}"],
        "error_message": message,
    }


class PreProcessNode(FunctionNode):
    """Screen the measurement record and validate the inspection profile.

    Trust gate: the caller must be at least VERIFIED_EXTERNAL — only an
    authenticated external system may submit records for inspection. The gate
    runs in ``BaseNode.__call__`` before ``execute()``.

    Raw measurement values are not written to State; the screened record text
    is, so later nodes can re-derive values inside their own ``execute()``.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")

        if not isinstance(user_input, str):
            return _reject(
                "measurement record must be text",
                "Invalid input type.",
                state,
            )

        if not user_input.strip():
            return _reject(
                "measurement record is empty",
                "Input is empty or missing.",
                state,
                code="EMPTY_INPUT",
            )

        record = user_input.strip()

        if len(record) > MAX_RECORD_CHARS:
            return _reject(
                "measurement record exceeds the size limit",
                "Measurement record too large; split the submission and resubmit.",
                state,
                code="QUESTION_TOO_LONG",
            )

        if _INJECTION_RE.search(record):
            return _reject(
                "injection pattern detected",
                "Input rejected: suspected injection pattern detected.",
                state,
                # Not something the caller corrects by rewording: the refusal
                # terminates the run rather than inviting another attempt.
                code="",
            )

        if _PII_RE.search(record):
            return _reject(
                "personal information detected",
                ("Input rejected: measurement records must not include " "personal information."),
                state,
                # A personal-information finding terminates: the record must not
                # be carried further, whatever the caller sends next.
                code="",
            )

        if "product_id" not in record.lower():
            return _reject(
                "missing required field product_id",
                "Input missing required field: product_id.",
                state,
            )

        bad_field, profile = validate_inspection_profile(state.get("input_context", {}))
        if bad_field is not None:
            return _reject(
                f"inspection profile field '{bad_field}' failed validation",
                f"Invalid value for inspection profile field '{bad_field}'.",
                state,
            )

        emit_trace_event(
            "pre_process_complete",
            {
                "input_length": len(record),
                "inspection_stage": profile["inspection_stage"],
                "tolerance_override_count": len(profile["tolerance_profile"]),
            },
            state,
        )
        return {
            "input_record": record,
            "part_family": profile["part_family"],
            "inspection_stage": profile["inspection_stage"],
            "status": AgentStatus.SUCCESS.value,
        }
