"""AgentCore Platform v1.0 — MFG-C2-006 inspection request contract.

One place that defines what an inspection request may contain, so every node
reads the same rules:

  * :func:`parse_measurement_record` — turn the submitted record (JSON object,
    JSON array of objects, or CSV) into a bounded list of measurement rows.
  * :func:`validate_inspection_profile` — validate the caller-supplied
    inspection profile that arrives on the ``input_context`` channel. Every
    field is checked against explicit bounds and fails CLOSED, naming the
    field and never echoing the rejected value.
  * :func:`load_specification_rules` / :func:`effective_rules` — the approved
    tolerance specification, and the effective rule set once a caller profile
    has been merged over it.

Privacy contract: measurement values and tolerance limits are handled inside
node ``execute()`` scopes and by this module only. Neither is ever written to
State, and the output gate redacts either of them from the rendered report.
"""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
from typing import Any

# ── Structural limits ────────────────────────────────────────────────────────
# A submission is bounded on every axis so that a hostile caller cannot turn a
# validation request into an unbounded computation.
MAX_RECORD_CHARS = 200_000
MAX_ROWS = 500
MAX_FIELDS_PER_ROW = 64

# ── Caller inspection profile (input_context) ────────────────────────────────
#
#   part_family        identifier [a-z0-9_]{1,32}   default ""       (reported)
#   inspection_stage   identifier [a-z0-9_]{1,32}   default "final"  (reported)
#   tolerance_profile  {identifier: {min, max}}     default {}       (merged)
#   cpk_threshold      finite number in [0, 1000]   default: specification
#
# Free text is not accepted on any field — identifiers only — so nothing the
# caller sends can carry renderable prose into the report. Keys outside this
# contract are ignored and cannot influence the pipeline.

IDENTIFIER_PATTERN = r"^[a-z0-9_]{1,32}$"

_MAX_TOLERANCE_ENTRIES = 32
_LIMIT_MIN = -1e9
_LIMIT_MAX = 1e9
_CPK_MIN = 0.0
_CPK_MAX = 1e3
_TOLERANCE_KEYS = {"min", "max", "unit"}

_DEFAULT_INSPECTION_STAGE = "final"

# The approved specification, used when config/specs.yaml is absent.
_DEFAULT_RULES: dict[str, Any] = {
    "dimensions": [
        {"field": "length", "min": 9.8, "max": 10.2, "unit": "mm"},
        {"field": "width", "min": 4.9, "max": 5.1, "unit": "mm"},
        {"field": "surface_roughness", "min": 0.0, "max": 1.6, "unit": "Ra"},
    ],
    "required_measurements": ["length", "width", "surface_roughness"],
    "iatf_required_fields": ["product_id", "lot_number"],
    "cpk_threshold": 1.33,
}

_IDENTIFIER_RE = re.compile(IDENTIFIER_PATTERN)


class ContractError(ValueError):
    """A submitted record breaks a structural limit of the request contract.

    Carries the *category* of the problem, never the offending value.
    """


def finite_in_range(value: Any, lo: float, hi: float) -> tuple[bool, float]:
    """Parse a caller-controlled numeric defensively; fail CLOSED.

    Accepts ``int``/``float`` only — ``bool`` is rejected explicitly because it
    is an ``int`` subclass. ``NaN`` and ``±Infinity`` parse fine as floats but
    every ordered comparison against them evaluates to False, which would
    silently disable any bound built on such a comparison, so non-finite values
    are rejected outright, as are values outside ``[lo, hi]``.

    Returns ``(ok, parsed)``; ``parsed`` is ``0.0`` whenever ``ok`` is False.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False, 0.0
    number = float(value)
    if not math.isfinite(number):
        return False, 0.0
    if number < lo or number > hi:
        return False, 0.0
    return True, number


def _is_identifier(value: Any) -> bool:
    """True when *value* is an inert identifier safe to render into output."""
    return isinstance(value, str) and bool(_IDENTIFIER_RE.match(value))


def parse_measurement_record(record: str) -> list[dict[str, str]]:
    """Parse a measurement submission into a bounded list of rows.

    Accepts a JSON object, a JSON array of objects, or CSV with a header row.
    Values are returned as strings; callers convert them under their own
    bounds. Raises :class:`ContractError` when the submission is malformed or
    exceeds a structural limit.
    """
    if not isinstance(record, str):
        raise ContractError("record must be text")

    text = record.strip()
    if not text:
        raise ContractError("record is empty")
    if len(text) > MAX_RECORD_CHARS:
        raise ContractError("record exceeds the size limit")

    rows: list[Any]
    if text.startswith("{") or text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ContractError(f"record is not valid JSON: {exc.msg}") from exc
        rows = data if isinstance(data, list) else [data]
    else:
        try:
            rows = list(csv.DictReader(io.StringIO(text)))
        except csv.Error as exc:
            raise ContractError(f"record is not valid CSV: {exc}") from exc

    if not rows:
        raise ContractError("record contains no measurement rows")
    if len(rows) > MAX_ROWS:
        raise ContractError("record exceeds the measurement-row limit")

    parsed: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError("every measurement row must be a mapping of field to value")
        if len(row) > MAX_FIELDS_PER_ROW:
            raise ContractError("a measurement row exceeds the field limit")
        parsed.append({str(key): "" if value is None else str(value) for key, value in row.items()})
    return parsed


def validate_inspection_profile(
    input_context: Any,
) -> tuple[str | None, dict[str, Any]]:
    """Validate the caller-supplied inspection profile.

    Returns ``(bad_field, profile)``. ``bad_field`` is the name of the first
    field that failed validation — the caller-supplied value itself is never
    returned, logged, or rendered — and is ``None`` when the profile is valid.
    An absent field takes its documented default, so a caller that sends no
    profile at all gets the approved specification unchanged.
    """
    profile: dict[str, Any] = {
        "part_family": "",
        "inspection_stage": _DEFAULT_INSPECTION_STAGE,
        "tolerance_profile": {},
        "cpk_threshold": None,
    }

    if input_context is None:
        return None, profile
    if not isinstance(input_context, dict):
        return "input_context", profile

    if "part_family" in input_context:
        if not _is_identifier(input_context["part_family"]):
            return "part_family", profile
        profile["part_family"] = input_context["part_family"]

    if "inspection_stage" in input_context:
        if not _is_identifier(input_context["inspection_stage"]):
            return "inspection_stage", profile
        profile["inspection_stage"] = input_context["inspection_stage"]

    if "cpk_threshold" in input_context:
        ok, threshold = finite_in_range(input_context["cpk_threshold"], _CPK_MIN, _CPK_MAX)
        if not ok:
            return "cpk_threshold", profile
        profile["cpk_threshold"] = threshold

    if "tolerance_profile" in input_context:
        raw_profile = input_context["tolerance_profile"]
        if not isinstance(raw_profile, dict) or len(raw_profile) > _MAX_TOLERANCE_ENTRIES:
            return "tolerance_profile", profile
        bounds: dict[str, dict[str, float]] = {}
        for field, band in raw_profile.items():
            if not _is_identifier(field):
                return "tolerance_profile", profile
            if not isinstance(band, dict) or not _TOLERANCE_KEYS.issuperset(band):
                return f"tolerance_profile.{field}", profile
            if "min" not in band or "max" not in band:
                return f"tolerance_profile.{field}", profile
            ok_min, low = finite_in_range(band["min"], _LIMIT_MIN, _LIMIT_MAX)
            ok_max, high = finite_in_range(band["max"], _LIMIT_MIN, _LIMIT_MAX)
            if not ok_min or not ok_max or low > high:
                return f"tolerance_profile.{field}", profile
            if "unit" in band and not _is_identifier(band["unit"]):
                return f"tolerance_profile.{field}", profile
            bounds[field] = {"min": low, "max": high}
        profile["tolerance_profile"] = bounds

    return None, profile


def load_specification_rules() -> dict[str, Any]:
    """Load the approved tolerance specification, or fall back to the defaults."""
    specs_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        "config",
        "specs.yaml",
    )
    if not os.path.exists(specs_path):
        return _DEFAULT_RULES
    try:
        import yaml

        with open(specs_path, encoding="utf-8") as handle:
            loaded = yaml.safe_load(handle) or {}
    except Exception:
        return _DEFAULT_RULES
    rules = loaded.get("tolerance_rules") if isinstance(loaded, dict) else None
    return rules if isinstance(rules, dict) else _DEFAULT_RULES


def effective_rules(profile: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Merge a validated caller profile over the approved specification.

    Returns ``(rules, overridden)`` where ``overridden`` lists — in sorted
    order — the dimension names whose limits or threshold came from the caller
    rather than from the approved specification. That list is disclosed in the
    report so an inspection result can never be mistaken for one evaluated
    against the approved control plan when it was not.

    The set of REQUIRED measurements is deliberately not caller-controlled: it
    is the approved control plan, and a caller must not be able to make a
    missing measurement disappear.
    """
    specification = load_specification_rules()
    dimensions: list[dict[str, Any]] = [dict(dim) for dim in specification.get("dimensions", [])]
    by_field = {str(dim.get("field", "")): dim for dim in dimensions}
    overridden: list[str] = []

    for field, band in profile.get("tolerance_profile", {}).items():
        overridden.append(field)
        if field in by_field:
            by_field[field]["min"] = band["min"]
            by_field[field]["max"] = band["max"]
        else:
            new_dimension = {"field": field, "min": band["min"], "max": band["max"]}
            dimensions.append(new_dimension)
            by_field[field] = new_dimension

    threshold = profile.get("cpk_threshold")
    if threshold is None:
        ok, threshold = finite_in_range(specification.get("cpk_threshold"), _CPK_MIN, _CPK_MAX)
        if not ok:
            threshold = float(_DEFAULT_RULES["cpk_threshold"])
    else:
        overridden.append("cpk_threshold")

    rules: dict[str, Any] = {
        "dimensions": dimensions,
        "required_measurements": list(specification.get("required_measurements", [])),
        "iatf_required_fields": list(specification.get("iatf_required_fields", [])),
        "cpk_threshold": float(threshold),
    }
    return rules, sorted(set(overridden))


def numeric_values(rows: list[dict[str, str]]) -> set[float]:
    """Every finite numeric value present in the submitted measurement rows.

    Used by the output gate to prove that no submitted value was reproduced in
    the report; the values themselves never leave the calling scope.
    """
    values: set[float] = set()
    for row in rows:
        for raw in row.values():
            try:
                number = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number):
                values.add(number)
    return values


def rule_values(rules: dict[str, Any]) -> set[float]:
    """Every finite limit in an effective rule set (tolerance bands, threshold)."""
    values: set[float] = set()
    for dimension in rules.get("dimensions", []):
        for key in ("min", "max"):
            ok, number = finite_in_range(dimension.get(key), _LIMIT_MIN, _LIMIT_MAX)
            if ok:
                values.add(number)
    ok, threshold = finite_in_range(rules.get("cpk_threshold"), _CPK_MIN, _CPK_MAX)
    if ok:
        values.add(threshold)
    return values
