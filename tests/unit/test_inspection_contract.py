"""MFG-C2-006 — unit tests for the inspection request contract.

Covers the two hostile surfaces of a request:

  * the measurement record — malformed text, structural limits;
  * the inspection profile on the input_context channel — every field bounded,
    every numeric finite, every rendered string inert, and every rejection
    fails CLOSED without echoing the value.
"""

import json

import pytest

from src.nodes.inspection_contract import (
    MAX_FIELDS_PER_ROW,
    MAX_RECORD_CHARS,
    MAX_ROWS,
    ContractError,
    effective_rules,
    finite_in_range,
    numeric_values,
    parse_measurement_record,
    rule_values,
    validate_inspection_profile,
)

_ECHO_MARKER = "zqx_echo_marker_zqx"

# Values that parse as floats but must never be accepted: every ordered
# comparison against a NaN is False, so a NaN bound silently disables itself.
_NON_FINITE = [float("nan"), float("inf"), float("-inf")]


class TestFiniteInRange:
    @pytest.mark.parametrize("value", _NON_FINITE)
    def test_non_finite_rejected(self, value):
        ok, parsed = finite_in_range(value, 0.0, 100.0)
        assert ok is False
        assert parsed == 0.0

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_rejected(self, value):
        assert finite_in_range(value, 0.0, 100.0)[0] is False

    @pytest.mark.parametrize("value", ["1.0", None, [1], {"a": 1}, object()])
    def test_non_numeric_rejected(self, value):
        assert finite_in_range(value, 0.0, 100.0)[0] is False

    @pytest.mark.parametrize("value", [-0.001, 100.001, 1e30])
    def test_out_of_range_rejected(self, value):
        assert finite_in_range(value, 0.0, 100.0)[0] is False

    @pytest.mark.parametrize("value", [0.0, 1, 99.9, 100.0])
    def test_in_range_accepted(self, value):
        ok, parsed = finite_in_range(value, 0.0, 100.0)
        assert ok is True
        assert parsed == float(value)


class TestParseMeasurementRecord:
    def test_json_object_becomes_one_row(self):
        rows = parse_measurement_record('{"product_id": "P-1", "length": 10.0}')
        assert rows == [{"product_id": "P-1", "length": "10.0"}]

    def test_json_array_keeps_every_row(self):
        rows = parse_measurement_record('[{"length": 10.0}, {"length": 10.1}]')
        assert [row["length"] for row in rows] == ["10.0", "10.1"]

    def test_csv_header_and_rows(self):
        rows = parse_measurement_record("product_id,length\nP-1,10.0\nP-1,10.1")
        assert len(rows) == 2
        assert rows[0]["product_id"] == "P-1"

    @pytest.mark.parametrize(
        "record",
        ["", "   ", "{not json}", "[1, 2, 3]", '{"a": 1'],
    )
    def test_malformed_record_refused(self, record):
        with pytest.raises(ContractError):
            parse_measurement_record(record)

    def test_non_text_record_refused(self):
        with pytest.raises(ContractError):
            parse_measurement_record({"length": 10.0})

    def test_oversized_record_refused(self):
        with pytest.raises(ContractError):
            parse_measurement_record("x" * (MAX_RECORD_CHARS + 1))

    def test_too_many_rows_refused(self):
        payload = json.dumps([{"length": 10.0}] * (MAX_ROWS + 1))
        with pytest.raises(ContractError):
            parse_measurement_record(payload)

    def test_too_many_fields_refused(self):
        payload = json.dumps({f"f{i}": 1 for i in range(MAX_FIELDS_PER_ROW + 1)})
        with pytest.raises(ContractError):
            parse_measurement_record(payload)

    def test_row_limit_boundary_accepted(self):
        payload = json.dumps([{"length": 10.0}] * MAX_ROWS)
        assert len(parse_measurement_record(payload)) == MAX_ROWS


class TestValidateInspectionProfile:
    def test_absent_profile_takes_defaults(self):
        bad, profile = validate_inspection_profile({})
        assert bad is None
        assert profile == {
            "part_family": "",
            "inspection_stage": "final",
            "tolerance_profile": {},
            "cpk_threshold": None,
        }

    def test_none_profile_takes_defaults(self):
        assert validate_inspection_profile(None)[0] is None

    def test_valid_profile_accepted(self):
        bad, profile = validate_inspection_profile(
            {
                "part_family": "bearing_6205",
                "inspection_stage": "incoming",
                "tolerance_profile": {"length": {"min": 9.9, "max": 10.1, "unit": "mm"}},
                "cpk_threshold": 1.67,
            }
        )
        assert bad is None
        assert profile["part_family"] == "bearing_6205"
        assert profile["tolerance_profile"]["length"] == {"min": 9.9, "max": 10.1}
        assert profile["cpk_threshold"] == 1.67

    @pytest.mark.parametrize("field", ["part_family", "inspection_stage"])
    @pytest.mark.parametrize(
        "value",
        ["Not An Identifier", "UPPER", "has space", "x" * 33, "", 7, None, ["a"]],
    )
    def test_rendered_strings_locked_to_identifiers(self, field, value):
        bad, _profile = validate_inspection_profile({field: value})
        assert bad == field

    @pytest.mark.parametrize("value", _NON_FINITE + [True, "1.33", None, -0.1, 1001.0])
    def test_cpk_threshold_must_be_finite_and_bounded(self, value):
        assert validate_inspection_profile({"cpk_threshold": value})[0] == "cpk_threshold"

    @pytest.mark.parametrize("value", _NON_FINITE + [True, "9.9", None, -1e10, 1e10])
    def test_tolerance_limits_must_be_finite_and_bounded(self, value):
        bad, _profile = validate_inspection_profile({"tolerance_profile": {"length": {"min": value, "max": 10.0}}})
        assert bad == "tolerance_profile.length"
        bad, _profile = validate_inspection_profile({"tolerance_profile": {"length": {"min": 9.0, "max": value}}})
        assert bad == "tolerance_profile.length"

    def test_inverted_band_refused(self):
        bad, _profile = validate_inspection_profile({"tolerance_profile": {"length": {"min": 10.5, "max": 9.5}}})
        assert bad == "tolerance_profile.length"

    @pytest.mark.parametrize(
        "band",
        [
            {"min": 9.0},
            {"max": 10.0},
            {"min": 9.0, "max": 10.0, "extra": 1},
            {"min": 9.0, "max": 10.0, "unit": "MM"},
            [9.0, 10.0],
            "9-10",
        ],
    )
    def test_malformed_band_refused(self, band):
        bad, _profile = validate_inspection_profile({"tolerance_profile": {"length": band}})
        assert bad == "tolerance_profile.length"

    def test_non_identifier_dimension_name_refused(self):
        bad, _profile = validate_inspection_profile({"tolerance_profile": {"Length Of Part": {"min": 1.0, "max": 2.0}}})
        assert bad == "tolerance_profile"

    def test_too_many_dimensions_refused(self):
        profile = {f"d{i}": {"min": 0.0, "max": 1.0} for i in range(33)}
        assert validate_inspection_profile({"tolerance_profile": profile})[0] == "tolerance_profile"

    @pytest.mark.parametrize("value", ["not a dict", 7, ["a"]])
    def test_non_mapping_context_refused(self, value):
        assert validate_inspection_profile(value)[0] == "input_context"

    def test_rejection_never_returns_the_value(self):
        bad, profile = validate_inspection_profile({"part_family": _ECHO_MARKER.upper()})
        assert bad == "part_family"
        assert _ECHO_MARKER not in json.dumps(profile).lower()

    def test_unknown_keys_are_ignored(self):
        bad, profile = validate_inspection_profile({"unknown_key": _ECHO_MARKER})
        assert bad is None
        assert _ECHO_MARKER not in json.dumps(profile)


class TestEffectiveRules:
    def test_specification_used_when_no_profile(self):
        rules, overridden = effective_rules(validate_inspection_profile({})[1])
        assert overridden == []
        assert {dim["field"] for dim in rules["dimensions"]} >= {"length", "width", "surface_roughness"}
        assert rules["cpk_threshold"] > 0

    def test_caller_limits_replace_specification_and_are_disclosed(self):
        _bad, profile = validate_inspection_profile(
            {"tolerance_profile": {"length": {"min": 1.0, "max": 2.0}}, "cpk_threshold": 2.0}
        )
        rules, overridden = effective_rules(profile)
        length = next(dim for dim in rules["dimensions"] if dim["field"] == "length")
        assert (length["min"], length["max"]) == (1.0, 2.0)
        assert rules["cpk_threshold"] == 2.0
        assert overridden == ["cpk_threshold", "length"]

    def test_caller_may_add_a_dimension(self):
        _bad, profile = validate_inspection_profile({"tolerance_profile": {"bore_diameter": {"min": 3.0, "max": 3.2}}})
        rules, overridden = effective_rules(profile)
        assert "bore_diameter" in {dim["field"] for dim in rules["dimensions"]}
        assert overridden == ["bore_diameter"]

    def test_required_measurements_are_not_caller_controlled(self):
        _bad, profile = validate_inspection_profile({"tolerance_profile": {"length": {"min": 1.0, "max": 2.0}}})
        rules, _overridden = effective_rules(profile)
        assert "length" in rules["required_measurements"]


class TestValueCollection:
    def test_numeric_values_ignores_non_numeric_fields(self):
        rows = parse_measurement_record('{"product_id": "P-1", "length": 10.0, "note": "abc"}')
        assert numeric_values(rows) == {10.0}

    def test_numeric_values_skips_non_finite_readings(self):
        rows = [{"length": "NaN", "width": "Infinity", "height": "3.5"}]
        assert numeric_values(rows) == {3.5}

    def test_rule_values_collects_limits_and_threshold(self):
        rules, _overridden = effective_rules(validate_inspection_profile({})[1])
        values = rule_values(rules)
        assert values
        assert all(isinstance(value, float) for value in values)
