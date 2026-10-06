# PB-7: human-in-the-loop interrupt-propagation boundary test.
#
# PB-7 verifies that an interrupt raised inside the agent graph propagates out
# to the invoking caller, so an orchestrator can pause the run, collect a human
# decision and resume it. That behaviour exists only in templates that opt into
# it: a main-slot GraphNode declaring `propagate_hitl = True`, or an explicit
# interrupt() checkpoint on the backbone.
#
# This template does neither. Its inspection pipeline is deterministic and runs
# to completion in one pass, so there is no propagation behaviour to assert and
# PB-7 ships as a skip: a real, importable module that skips with a clear
# reason (never `assert True`), ready to be filled in if a future revision
# wires an interrupt checkpoint.

import importlib

import pytest


def _hitl_propagation_enabled() -> bool:
    """True iff this template opts into cross-boundary interrupt propagation.

    Detected by inspecting the classes DEFINED in ``src/graph/graph.py`` for a
    node or graph subclass declaring ``propagate_hitl = True``. Imported
    defensively so collection never errors when the SDK or the graph module is
    unavailable — the test then simply skips.
    """
    try:
        graph_mod = importlib.import_module("src.graph.graph")
    except Exception:
        return False
    for obj in vars(graph_mod).values():
        if (
            isinstance(obj, type)
            and getattr(obj, "__module__", None) == graph_mod.__name__
            and getattr(obj, "propagate_hitl", False) is True
        ):
            return True
    return False


_HITL_PROPAGATION_ENABLED = _hitl_propagation_enabled()

_PB7_SKIP_REASON = (
    "interrupt propagation is not implemented for this template: no graph "
    "class declares propagate_hitl=True and the backbone has no interrupt() "
    "checkpoint, so there is no propagation behaviour to assert"
)


@pytest.mark.skipif(not _HITL_PROPAGATION_ENABLED, reason=_PB7_SKIP_REASON)
class TestPB7HitlInterruptPropagation:
    """PB-7: an interrupt must propagate across the graph boundary.

    Skipped for this template — propagation is not enabled, so there is no
    behaviour to verify. The real assertion belongs here once an interrupt
    checkpoint is wired end-to-end.
    """

    def test_hitl_interrupt_propagates_to_caller(self):
        # Reached only when a graph class declares propagate_hitl=True. The
        # real assertion (invoke -> the interrupt surfaces to the caller ->
        # resume) is written at that point.
        raise AssertionError("interrupt-propagation assertion not yet implemented for this template")
