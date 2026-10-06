"""AgentCore Platform v1.0 — MFG-C2-006 PostProcessNode.

The output boundary. Two independent layers run here, in this order, and each
raises its own audit event:

  1. **Screen** — the report is scanned for credential-shaped and
     personal-information-shaped content. A hit BLOCKS the response: nothing
     is published and the node returns an error.
  2. **Redact** — every numeric token in the report is compared with the
     values the request actually carried (the submitted readings and the
     effective tolerance limits). A token that reproduces one of them is
     replaced with ``[REDACTED]``. This enforces the reporting schema stated
     in the report itself — verdicts and dimension names only — independently
     of whether the report builder happened to honour it.

The screen runs BEFORE the redaction and again after it. A redaction rewrites
digits, and a structured secret is recognised by the shape of its digits, so
redacting first could destroy the very pattern the screen exists to catch;
screening first means a national-identification number is blocked, never
quietly mangled into something that looks harmless.

Identifiers are protected from the redaction pass: a numeric that sits inside
a dotted, hyphenated or underscored run containing a letter — part numbers,
lot codes, the template identifier — is part of a name, not a reading, and is
left byte-identical. The rule is structural, so it needs no list of the
identifier formats a given plant happens to use.

Audit: emits 'post_process_complete', 'post_process_error', or
'post_process_redacted'.

Writes: formatted_output, status, error_message / error_log (on rejection).
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from src.nodes.inspection_contract import (
    ContractError,
    effective_rules,
    numeric_values,
    parse_measurement_record,
    rule_values,
    validate_inspection_profile,
)

# Layer 1 — content that must never leave the boundary at all.
_BLOCKED_CONTENT: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("api_key", re.compile(r"\b(?:sk|pk|ak)-[A-Za-z0-9]{16,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer_token", re.compile(r"bearer\s+[A-Za-z0-9\-._~+/]{16,}=*", re.IGNORECASE)),
    (
        "credential_assignment",
        re.compile(
            r"\b(?:password|passwd|secret|api[_-]?key|token|access[_-]?key|private[_-]?key)" r"\s*[:=]\s*\S+",
            re.IGNORECASE,
        ),
    ),
    ("national_id", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("payment_card", re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")),
    ("email_address", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
)

# Layer 2 — telling a NAME from a READING.
#
# A run of alphanumerics joined by . - _ / is scanned unless it is a name. The
# test is structural and biased towards scanning: a run counts as a reading
# when it is built only from numbers, those separators, and an optional short
# unit suffix ("10.05", "10.05mm", "1.6Ra", "9.8-10.2", "1e-05"). Anything
# else — "SKF-6205", "LOT-2026-001", "MFG-C2-006", "bearing_6205" — is a name,
# and the numbers inside it are left byte-identical. Erring towards scanning
# matters: mistaking a reading for a name would leak it, while mistaking a
# name for a reading can only redact a token that happens to equal a submitted
# value, which fails closed.
_RUN_RE = re.compile(r"[A-Za-z0-9]+(?:[.\-_/][A-Za-z0-9]+)+")
_NUMBER = r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_READING_RUN_RE = re.compile(rf"^{_NUMBER}(?:[.\-_/]{_NUMBER})*[A-Za-z]{{0,6}}$")

# A numeric token. The lookbehind keeps a leading sign from being consumed when
# it is really a range dash or a decimal continuation ("9.8-10.2" is two
# tokens, " -10.05" is one signed token).
_VALUE_TOKEN_RE = re.compile(rf"(?<![\d.]){_NUMBER}")

_REDACTION = "[REDACTED]"


def screen_blocked_content(text: str) -> str | None:
    """Return the name of the first blocked content class found, else None."""
    for name, pattern in _BLOCKED_CONTENT:
        if pattern.search(text):
            return name
    return None


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """Character spans that belong to a name rather than to a reading."""
    return [match.span() for match in _RUN_RE.finditer(text) if not _READING_RUN_RE.match(match.group(0))]


def disclosed_values_in(text: str, values: set[float]) -> list[str]:
    """Numeric tokens in *text* that reproduce one of *values*.

    The report is compliant with its own stated schema exactly when this
    returns an empty list; the tests assert that on the real output.
    """
    if not values:
        return []
    magnitudes = {abs(value) for value in values}
    spans = _protected_spans(text)
    found: list[str] = []
    for match in _VALUE_TOKEN_RE.finditer(text):
        if any(start <= match.start() < end for start, end in spans):
            continue
        try:
            number = float(match.group(0))
        except ValueError:  # pragma: no cover - the pattern only matches numerics
            continue
        if abs(number) in magnitudes:
            found.append(match.group(0))
    return found


def redact_disclosed_values(text: str, values: set[float]) -> tuple[str, int]:
    """Replace every reproduced reading or tolerance limit with a redaction.

    Returns ``(text, redaction_count)``.
    """
    if not values:
        return text, 0
    magnitudes = {abs(value) for value in values}
    spans = _protected_spans(text)
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        if any(start <= match.start() < end for start, end in spans):
            return match.group(0)
        try:
            number = float(match.group(0))
        except ValueError:  # pragma: no cover - the pattern only matches numerics
            return match.group(0)
        if abs(number) not in magnitudes:
            return match.group(0)
        count += 1
        return _REDACTION

    return _VALUE_TOKEN_RE.sub(replace, text), count


def request_values(input_record: str, input_context: Any) -> set[float]:
    """Every number this request carried: submitted readings and the limits.

    Neither set is held in State, so the gate re-derives both from the screened
    record and the caller profile. A request that cannot be re-read yields an
    empty set — the screen layer still applies.
    """
    values: set[float] = set()
    try:
        rows = parse_measurement_record(input_record)
    except ContractError:
        rows = []
    values |= numeric_values(rows)

    bad_field, profile = validate_inspection_profile(input_context)
    if bad_field is None:
        rules, _overridden = effective_rules(profile)
        values |= rule_values(rules)
    return values


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Screen, redact and format the inspection report for the caller.

    Reads ``state["report_output"]`` and re-derives the request's own numbers
    from ``input_record`` / ``input_context`` to enforce the reporting schema.

    Trust level: ANONYMOUS — the caller trust gate runs at the pre_process slot.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "formatted_output": message,
            }
        report_output = state.get("report_output", "")

        blocked = screen_blocked_content(report_output)
        if blocked is not None:
            emit_trace_event(
                "post_process_error",
                {"reason": "blocked_content", "content_class": blocked},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PostProcessNode: output blocked, content class '{blocked}'"],
                "error_message": "Report blocked: disallowed content detected in the generated report.",
            }

        values = request_values(state.get("input_record", ""), state.get("input_context", {}))
        cleaned, redactions = redact_disclosed_values(report_output, values)

        # Screen again: the redaction rewrote digits, so re-run the layer that
        # recognises content by its digit shape.
        blocked = screen_blocked_content(cleaned)
        if blocked is not None:
            emit_trace_event(
                "post_process_error",
                {"reason": "blocked_content_after_redaction", "content_class": blocked},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PostProcessNode: output blocked, content class '{blocked}'"],
                "error_message": "Report blocked: disallowed content detected in the generated report.",
            }

        if redactions:
            emit_trace_event(
                "post_process_redacted",
                {"redaction_count": redactions},
                state,
            )

        overall_status = state.get("overall_status", "UNKNOWN")
        formatted_output = f"[MFG-C2-006 QC Inspection Result]\nDisposition: {overall_status}\n\n{cleaned}"

        emit_trace_event(
            "post_process_complete",
            {
                "output_length": len(formatted_output),
                "overall_status": overall_status,
                "redaction_count": redactions,
            },
            state,
        )
        return {
            "formatted_output": formatted_output,
            "status": AgentStatus.SUCCESS.value,
        }
