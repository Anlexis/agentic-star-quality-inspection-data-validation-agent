"""MFG-C2-006 — unit tests for the output boundary.

The report states its own schema: verdicts and dimension names only, no
measured value and no tolerance limit reproduced. These tests hold the gate to
that statement for EVERY way a number can be written, and hold it the other
way too — names that merely contain digits must come through byte-identical.

They also pin the layer ORDER. The redaction rewrites digits and a structured
secret is recognised by the shape of its digits, so the screen has to run
first or a national-identification number would be mangled into something the
screen no longer recognises, and the report would ship.
"""

from unittest.mock import patch

import pytest
from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import (
    PostProcessNode,
    disclosed_values_in,
    redact_disclosed_values,
    request_values,
    screen_blocked_content,
)

# One submitted reading and one tolerance limit, used across the matrices.
_VALUES = {10.05, 1.6}


class TestBlockedContentScreen:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("config sk-abcdefghijklmnopqrstuvwxyz123456", "api_key"),
            ("auth eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27u", "jwt"),
            ("Authorization: Bearer abcdefghijklmnop0123456789", "bearer_token"),
            ("password = hunter2swordfish", "credential_assignment"),
            ("api_key: abcdef123456", "credential_assignment"),
            ("operator 123-45-6789", "national_id"),
            ("card 4111 1111 1111 1111", "payment_card"),
            ("contact inspector@example.com", "email_address"),
        ],
    )
    def test_blocked_classes_detected(self, text, expected):
        assert screen_blocked_content(text) == expected

    @pytest.mark.parametrize(
        "text",
        [
            "length: PASS (capability: PASS)",
            "Quality flags: OUT_OF_TOLERANCE",
            "Part family: bearing_6205",
            "lot LOT-2026-001 dispositioned",
            "IATF 16949 control plan consulted",
        ],
    )
    def test_ordinary_report_text_passes(self, text):
        assert screen_blocked_content(text) is None


class TestRedactionCoversEveryRepresentation:
    @pytest.mark.parametrize(
        "text",
        [
            "reading 10.05 mm",
            "reading 10.050 mm",
            "reading +10.05",
            "reading -10.05",
            "reading 10.05mm",
            "reading 10.05e0",
            "reading 1.005E+1",
            "reading\t10.05\t",
            "reading\n10.05\n",
            "reading (10.05)",
            "reading [10.05]",
            "reading=10.05",
            "band 10.05-99.9",
            "band 9.9-10.05",
        ],
    )
    def test_reading_is_redacted(self, text):
        cleaned, count = redact_disclosed_values(text, _VALUES)
        assert count >= 1, f"not redacted: {text!r} -> {cleaned!r}"
        assert "10.05" not in cleaned
        assert "1.005" not in cleaned
        assert disclosed_values_in(cleaned, _VALUES) == []

    def test_tolerance_limit_is_redacted_too(self):
        cleaned, count = redact_disclosed_values("upper limit 1.6 Ra", _VALUES)
        assert count == 1
        assert "1.6" not in cleaned

    def test_every_occurrence_is_redacted(self):
        cleaned, count = redact_disclosed_values("10.05 then 10.05 then 1.6", _VALUES)
        assert count == 3
        assert disclosed_values_in(cleaned, _VALUES) == []

    def test_no_magnitude_exemption(self):
        # A tiny reading and a large one are treated identically: the schema
        # says no value is reproduced, so magnitude never earns an exemption.
        values = {0.0001, 987654.0}
        for text in ("reading 0.0001 mm", "reading 1e-04 mm", "reading 987654 units"):
            cleaned, count = redact_disclosed_values(text, values)
            assert count == 1, f"not redacted: {text!r} -> {cleaned!r}"


class TestNamesAreNotReadings:
    @pytest.mark.parametrize(
        "text",
        [
            "part SKF-6205 inspected",
            "lot LOT-2026-001 dispositioned",
            "traveller MFG-FAC-20260712-001",
            "template MFG-C2-006",
            "part family bearing_6205",
            "form STU-1234 attached",
            "IATF 16949 control plan",
            "class ISO 2768-m",
            "Currency: JPY\n\n3. Cash Position",
        ],
    )
    def test_identifier_survives_byte_identical(self, text):
        # Every magnitude buried inside these names is in the value set, so a
        # gate without the name guard would rewrite them. Standalone numbers
        # that are not part of a name ("16949", the "3." section number) are
        # deliberately absent from the set: those are scanned like any other
        # number, and the next test pins that behaviour.
        values = {6205.0, 2026.0, 1.0, 20260712.0, 2.0, 6.0, 1234.0, 2768.0}
        cleaned, count = redact_disclosed_values(text, values)
        assert cleaned == text, f"identifier mangled: {text!r} -> {cleaned!r}"
        assert count == 0

    def test_a_standalone_number_is_scanned_wherever_it_sits(self):
        # Structural position earns no exemption: if a submitted reading is
        # reproduced as a section number, that is still a disclosure, and the
        # gate closes on it.
        text = "Currency: JPY\n\n3. Cash Position"
        assert redact_disclosed_values(text, {9.9})[0] == text
        assert redact_disclosed_values(text, {3.0})[1] == 1

    def test_a_reading_with_a_unit_suffix_is_still_a_reading(self):
        # The mirror image of the test above: letters attached to a number are
        # a unit, not a name, and must not buy protection.
        cleaned, count = redact_disclosed_values("reading 6205mm", {6205.0})
        assert count == 1
        assert "6205" not in cleaned


class TestLayerOrder:
    """The screen runs before the redaction — proven on the node itself."""

    @pytest.fixture(autouse=True)
    def patch_emit(self):
        with patch("src.nodes.post_process_node.emit_trace_event"):
            yield

    @pytest.mark.parametrize(
        "secret",
        ["SSN 123-45-6789", "TAX 987-65-4321", "card 4111 1111 1111 1111"],
    )
    def test_structured_secret_is_blocked_not_mangled(self, secret):
        # Every digit group of the secret is also a submitted reading, so a
        # redaction-first gate would rewrite the secret's shape and publish it.
        record = (
            '{"product_id": "P-1", "a": 123, "b": 45, "c": 6789, "d": 987, ' '"e": 65, "f": 4321, "g": 4111, "h": 1111}'
        )
        node = PostProcessNode()
        result = node.execute(
            {
                "report_output": f"QC Inspection Report\n{secret}\n",
                "input_record": record,
                "input_context": {},
                "overall_status": "PASS",
            }
        )
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_redaction_cannot_smuggle_a_secret_past_the_screen(self):
        # The screen runs again after the redaction, so a report that only
        # becomes secret-shaped once digits move is still blocked.
        node = PostProcessNode()
        result = node.execute(
            {
                "report_output": "QC Inspection Report\nref 123-45-6789 recorded\n",
                "input_record": '{"product_id": "P-1", "length": 10.05}',
                "input_context": {},
                "overall_status": "PASS",
            }
        )
        assert result["status"] == AgentStatus.ERROR.value


class TestPostProcessNode:
    @pytest.fixture(autouse=True)
    def patch_emit(self):
        with patch("src.nodes.post_process_node.emit_trace_event"):
            yield

    @pytest.fixture
    def node(self):
        return PostProcessNode()

    def test_clean_report_passes(self, node):
        result = node.execute(
            {
                "report_output": "QC Inspection Report\nlength: PASS (capability: PASS)",
                "input_record": '{"product_id": "P-1", "length": 10.0}',
                "input_context": {},
                "overall_status": "PASS",
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "MFG-C2-006" in result["formatted_output"]
        assert "Disposition: PASS" in result["formatted_output"]

    def test_leaked_reading_is_redacted_from_the_output(self, node):
        result = node.execute(
            {
                "report_output": "QC Inspection Report\nlength measured 10.05 mm",
                "input_record": '{"product_id": "P-1", "length": 10.05}',
                "input_context": {},
                "overall_status": "PASS",
            }
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "10.05" not in result["formatted_output"]
        assert "[REDACTED]" in result["formatted_output"]

    def test_caller_supplied_limit_is_redacted_too(self, node):
        result = node.execute(
            {
                "report_output": "QC Inspection Report\nupper limit was 12.75",
                "input_record": '{"product_id": "P-1", "length": 10.0}',
                "input_context": {"tolerance_profile": {"length": {"min": 1.0, "max": 12.75}}},
                "overall_status": "PASS",
            }
        )
        assert "12.75" not in result["formatted_output"]

    def test_writes_the_declared_state_key(self, node):
        result = node.execute(
            {
                "report_output": "QC Inspection Report",
                "input_record": '{"product_id": "P-1"}',
                "input_context": {},
                "overall_status": "PASS",
            }
        )
        assert "formatted_output" in result
        assert "output" not in result

    def test_trust_level_is_declared(self, node):
        from framework.schemas.trust_level import TrustLevel

        assert node.required_trust_level == TrustLevel.ANONYMOUS


class TestRequestValues:
    def test_collects_readings_and_effective_limits(self):
        values = request_values('{"product_id": "P-1", "length": 10.05}', {})
        assert 10.05 in values
        assert 9.8 in values  # specification lower limit for length

    def test_unreadable_record_yields_the_limits_only(self):
        values = request_values("{not json}", {})
        assert 10.05 not in values
        assert 9.8 in values

    def test_invalid_profile_contributes_nothing(self):
        values = request_values('{"product_id": "P-1"}', {"cpk_threshold": float("nan")})
        assert values == set()
