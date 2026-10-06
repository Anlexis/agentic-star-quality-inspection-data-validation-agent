"""AgentCore Platform v1.0 — MFG-C2-006 ToleranceValidateNode.

Evaluates the submitted measurement rows against the effective tolerance rule
set — the approved specification in ``config/specs.yaml``, with any validated
caller-supplied limits merged over it — and records a per-dimension verdict.

Two evaluations run per dimension:

  * conformance — every submitted value must sit inside the tolerance band;
  * capability  — when the submission carries at least two rows with spread,
    the process-capability index is computed for the dimension and compared
    with the effective threshold. Fewer rows, or zero spread, is reported as
    INSUFFICIENT_SAMPLES rather than silently passing.

Privacy contract: measurement values and tolerance limits are re-derived
locally from ``input_record`` and the rule set; only the derived verdicts
(PASS / FAIL / MISSING and the capability verdict) are written to State.

Audit: emits 'tolerance_validate_complete' or 'tolerance_validate_error'.

Writes: validation_results (JSON), tolerance_override_fields (JSON), status.
"""

from __future__ import annotations

import json
import math
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.nodes.inspection_contract import (
    ContractError,
    effective_rules,
    finite_in_range,
    parse_measurement_record,
    validate_inspection_profile,
)

# Measurement magnitude bound. A value outside it is not a plausible dimension
# and is treated as a malformed reading rather than a conforming one.
_MEASUREMENT_MIN = -1e12
_MEASUREMENT_MAX = 1e12

# Capability verdicts.
_CAPABILITY_PASS = "PASS"
_CAPABILITY_FAIL = "FAIL"
_CAPABILITY_INSUFFICIENT = "INSUFFICIENT_SAMPLES"


def _column(rows: list[dict[str, str]], field: str) -> tuple[list[float], bool, bool]:
    """Collect one dimension's readings across the submitted rows.

    Returns ``(values, present, malformed)``: the finite readings, whether the
    dimension appeared at all, and whether any reading present could not be
    read as a finite number within the plausible magnitude bound.
    """
    values: list[float] = []
    present = False
    malformed = False
    for row in rows:
        if field not in row:
            continue
        present = True
        raw = row[field]
        try:
            candidate = float(raw)
        except (TypeError, ValueError):
            malformed = True
            continue
        ok, number = finite_in_range(candidate, _MEASUREMENT_MIN, _MEASUREMENT_MAX)
        if not ok:
            malformed = True
            continue
        values.append(number)
    return values, present, malformed


def _capability(values: list[float], low: float, high: float, threshold: float) -> tuple[str, float | None]:
    """Process-capability verdict for one dimension.

    Needs at least two readings and non-zero spread; anything less is reported
    as INSUFFICIENT_SAMPLES rather than assumed capable. The index itself is
    returned for auditing and is never rendered into the report.
    """
    if len(values) < 2:
        return _CAPABILITY_INSUFFICIENT, None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    sigma = math.sqrt(variance)
    if sigma <= 0.0:
        return _CAPABILITY_INSUFFICIENT, None
    index = min(high - mean, mean - low) / (3.0 * sigma)
    return (_CAPABILITY_PASS if index >= threshold else _CAPABILITY_FAIL), index


class ToleranceValidateNode(FunctionNode):
    """Apply the effective tolerance rules to the submitted measurement rows.

    Reads ``state["input_record"]`` and the validated inspection profile on
    ``state["input_context"]``; writes only derived verdicts.

    Trust level: ANONYMOUS — operates on already-screened input.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        input_record = state.get("input_record", "")

        if not input_record:
            emit_trace_event(
                "tolerance_validate_error",
                {"reason": "empty_input_record"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ToleranceValidateNode: input_record is empty"],
                "error_message": "ToleranceValidateNode: input_record is empty.",
            }

        try:
            # Readings stay local to this call — never written to State.
            rows = parse_measurement_record(input_record)
        except ContractError as exc:
            emit_trace_event(
                "tolerance_validate_error",
                {"reason": "parse_failed", "detail": str(exc)},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ToleranceValidateNode: {exc}"],
                "error_message": f"ToleranceValidateNode: failed to re-read the record: {exc}.",
            }

        bad_field, profile = validate_inspection_profile(state.get("input_context", {}))
        if bad_field is not None:
            # PreProcessNode already refused this request; reaching here means the
            # profile changed under us, so refuse rather than evaluate.
            emit_trace_event(
                "tolerance_validate_error",
                {"reason": "invalid_inspection_profile", "field": bad_field},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"ToleranceValidateNode: inspection profile field '{bad_field}' failed validation"],
                "error_message": f"Invalid value for inspection profile field '{bad_field}'.",
            }

        rules, overridden = effective_rules(profile)
        threshold = float(rules["cpk_threshold"])
        results: dict[str, dict[str, str]] = {}

        for dimension in rules.get("dimensions", []):
            field = str(dimension.get("field", ""))
            if not field:
                continue
            ok_low, low = finite_in_range(dimension.get("min"), _MEASUREMENT_MIN, _MEASUREMENT_MAX)
            ok_high, high = finite_in_range(dimension.get("max"), _MEASUREMENT_MIN, _MEASUREMENT_MAX)
            if not ok_low or not ok_high or low > high:
                results[field] = {
                    "status": "FAIL",
                    "detail": "Tolerance band for this dimension is not usable.",
                    "capability": _CAPABILITY_INSUFFICIENT,
                }
                continue

            values, present, malformed = _column(rows, field)
            if not present:
                results[field] = {
                    "status": "MISSING",
                    "detail": "Dimension absent from the measurement record.",
                    "capability": _CAPABILITY_INSUFFICIENT,
                }
                continue
            if malformed:
                results[field] = {
                    "status": "FAIL",
                    "detail": "Reading is not a usable numeric measurement.",
                    "capability": _CAPABILITY_INSUFFICIENT,
                }
                continue

            conforming = all(low <= value <= high for value in values)
            capability, _index = _capability(values, low, high, threshold)
            results[field] = {
                "status": "PASS" if conforming else "FAIL",
                "detail": (
                    "All readings inside the tolerance band."
                    if conforming
                    else "At least one reading outside the tolerance band."
                ),
                "capability": capability,
            }

        # A required measurement with no rule of its own still has to be present.
        for required in rules.get("required_measurements", []):
            field = str(required)
            if field in results:
                continue
            present = any(field in row for row in rows)
            results[field] = {
                "status": "PASS" if present else "MISSING",
                "detail": (
                    "Required measurement present." if present else "Required measurement absent from the record."
                ),
                "capability": _CAPABILITY_INSUFFICIENT,
            }

        fail_count = sum(1 for verdict in results.values() if verdict["status"] != "PASS")
        capability_fail_count = sum(1 for verdict in results.values() if verdict["capability"] == _CAPABILITY_FAIL)

        emit_trace_event(
            "tolerance_validate_complete",
            {
                "dimensions_checked": len(results),
                "fail_count": fail_count,
                "capability_fail_count": capability_fail_count,
                "record_count": len(rows),
                "caller_overridden": overridden,
            },
            state,
        )
        return {
            "validation_results": json.dumps(results),
            "tolerance_override_fields": json.dumps(overridden),
            "status": AgentStatus.SUCCESS.value,
        }
