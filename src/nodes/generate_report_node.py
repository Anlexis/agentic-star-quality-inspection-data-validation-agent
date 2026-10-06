"""AgentCore Platform v1.0 — MFG-C2-006 GenerateReportNode.

Compiles the per-dimension verdicts and quality flags into the inspection
report: an overall disposition, the per-dimension result table, the source of
the tolerance limits that produced it, and remediation guidance for anything
that did not pass.

Reporting schema (enforced independently by the output gate in
PostProcessNode): the report carries verdicts and field names only. No
measured value and no tolerance limit is reproduced in it, so a report can be
circulated without disclosing either the readings or the specification they
were judged against.

Audit: emits 'generate_report_complete'.

Writes: overall_status, remediation_suggestions, report_output, status.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

# Remediation guidance keyed by verdict — actionable text, no values.
_REMEDIATION: dict[str, str] = {
    "FAIL": (
        "Re-measure '{field}' with a calibrated instrument, verify fixture "
        "alignment, and repeat the measurement before dispositioning the lot."
    ),
    "MISSING": (
        "Dimension '{field}' is absent from the record. Confirm the inspection "
        "form covers every dimension in the control plan before resubmitting."
    ),
    "CAPABILITY": (
        "Process capability for '{field}' is below the required threshold. "
        "Review tooling wear and process centring; a conforming sample set "
        "does not by itself demonstrate a capable process."
    ),
}

_SCHEMA_NOTE = (
    "Reporting schema: verdicts and dimension names only — measured values and "
    "tolerance limits are not reproduced in this report."
)


class GenerateReportNode(FunctionNode):
    """Compile the verdicts into the inspection report.

    Reads validation_results, qc_flags, tolerance_override_fields,
    part_family and inspection_stage; writes the disposition, the remediation
    text and the report body.

    Trust level: ANONYMOUS — operates on derived verdicts only.
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        try:
            results: dict[str, dict[str, str]] = json.loads(state.get("validation_results", "{}"))
            flags: list[str] = json.loads(state.get("qc_flags", "[]"))
            overridden: list[str] = json.loads(state.get("tolerance_override_fields", "[]"))
        except (json.JSONDecodeError, TypeError):
            results, flags, overridden = {}, [], []

        # ── Disposition ──────────────────────────────────────────────────────
        if "OUT_OF_TOLERANCE" in flags or "METADATA_INCOMPLETE" in flags:
            overall_status = "FAIL"
        elif flags:
            # Missing measurements or a capability shortfall: the lot cannot be
            # released on this evidence, but nothing measured was out of band.
            overall_status = "CONDITIONAL"
        else:
            overall_status = "PASS"

        # ── Remediation ──────────────────────────────────────────────────────
        remediation_lines: list[str] = []
        for field, verdict in sorted(results.items()):
            status = verdict.get("status", "")
            if status in _REMEDIATION:
                remediation_lines.append(_REMEDIATION[status].format(field=field))
            if verdict.get("capability") == "FAIL":
                remediation_lines.append(_REMEDIATION["CAPABILITY"].format(field=field))

        remediation_text = (
            "\n".join(remediation_lines) if remediation_lines else "No remediation required; every dimension conforms."
        )

        # ── Report body ──────────────────────────────────────────────────────
        tolerance_source = (
            f"caller-supplied profile ({', '.join(overridden)}) merged over the approved specification"
            if overridden
            else "approved specification"
        )
        part_family = state.get("part_family") or "unspecified"
        inspection_stage = state.get("inspection_stage") or "unspecified"

        lines: list[str] = [
            "QC Inspection Report",
            "=" * 40,
            f"Disposition: {overall_status}",
            f"Inspection stage: {inspection_stage}",
            f"Part family: {part_family}",
            f"Tolerance source: {tolerance_source}",
            f"Quality flags: {', '.join(flags) if flags else 'none'}",
            "",
            _SCHEMA_NOTE,
            "",
            "Dimension results:",
        ]
        for field, verdict in sorted(results.items()):
            capability = verdict.get("capability", "INSUFFICIENT_SAMPLES")
            lines.append(f"  {field}: {verdict.get('status', 'UNKNOWN')} (capability: {capability})")

        if remediation_lines:
            lines += ["", "Remediation required:"]
            lines += [f"  - {line}" for line in remediation_lines]

        report_output = "\n".join(lines)

        emit_trace_event(
            "generate_report_complete",
            {
                "overall_status": overall_status,
                "flag_count": len(flags),
                "remediation_count": len(remediation_lines),
                "caller_overridden": overridden,
            },
            state,
        )
        return {
            "overall_status": overall_status,
            "remediation_suggestions": remediation_text,
            "report_output": report_output,
            "status": AgentStatus.SUCCESS.value,
        }
