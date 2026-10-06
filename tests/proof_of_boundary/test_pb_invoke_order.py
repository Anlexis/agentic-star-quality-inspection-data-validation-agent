"""MFG-C2-006 — PB-6: backbone invoke-order verification.

A full ``Graph().invoke()`` with an external, authenticated caller must run the
backbone in order:

    InitializeNode -> PreProcessNode -> QCInspectionPipelineNode
    -> PostProcessNode -> FinalizeNode

The caller context must be VERIFIED_EXTERNAL, not an internal one: an internal
context outranks every inner node's ANONYMOUS requirement, so the trust gate
always passes and the path a production caller actually takes is never
exercised.

The success path is required to see the whole order — a non-success status
short-circuits main -> post_process, so node_history would be truncated.
"""

import importlib

import pytest

# The class in the `main` slot of graph.py. This template composes the domain
# steps inside one flat node rather than an inner subgraph.
_MAIN_SLOT_NODE = "QCInspectionPipelineNode"

# A submission that yields SUCCESS: every reading inside the specification
# bands (length [9.8, 10.2], width [4.9, 5.1], surface roughness [0.0, 1.6]),
# product_id present, no personal information, no injection content.
# Kept identical to deploy/invoke_payload.json.
_VALID_PAYLOAD = (
    '{"product_id": "PART-QC-001", "lot_number": "LOT-2026-001", '
    '"length": 10.0, "width": 5.0, "surface_roughness": 0.8}'
)


def _try_import(path):
    """Import a module, skipping the test if the SDK is not installed."""
    try:
        return importlib.import_module(path)
    except ImportError as exc:
        pytest.skip(f"SDK not installed — {path}: {exc}")


def _agent_and_context(caller_id):
    graph_mod = _try_import("src.graph.graph")
    fw_ctx = _try_import("framework.schemas.invocation_context")
    fw_tl = _try_import("framework.schemas.trust_level")

    agent = graph_mod.MfgC2006Agent()
    agent.compile()
    ctx = fw_ctx.InvocationContext(
        caller_trust_level=fw_tl.TrustLevel.VERIFIED_EXTERNAL,
        caller_id=caller_id,
        session_id="pb6-session-mfg-c2-006",
    )
    return agent, ctx


class TestPB6BackboneInvokeOrder:
    """PB-6: the backbone runs its five nodes in the fixed order."""

    def test_backbone_invoke_order_verified_external(self):
        agent, ctx = _agent_and_context("pb6-test")
        AgentStatus = _try_import("framework.schemas.agent_status").AgentStatus

        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)

        assert result.get("status") == AgentStatus.SUCCESS.value, (
            f"an authenticated external caller must succeed; got status="
            f"{result.get('status')!r} output={result.get('output', '')[:200]!r}"
        )

        node_history = result.get("node_history", [])
        assert len(node_history) >= 5, f"expected the five backbone nodes; got {node_history}"

        expected_order = [
            "InitializeNode",
            "PreProcessNode",
            _MAIN_SLOT_NODE,
            "PostProcessNode",
            "FinalizeNode",
        ]
        history = " ".join(str(entry) for entry in node_history)
        for node_name in expected_order:
            assert node_name in history, f"'{node_name}' missing from node_history: {node_history}"

        positions = {}
        for node_name in expected_order:
            for index, entry in enumerate(node_history):
                if node_name in str(entry):
                    positions[node_name] = index
                    break
        for earlier, later in zip(expected_order, expected_order[1:]):
            assert positions[earlier] < positions[later], (
                f"backbone order violation: '{earlier}' must precede '{later}'; " f"node_history: {node_history}"
            )

    def test_output_is_a_non_empty_string(self):
        agent, ctx = _agent_and_context("pb6-output-test")
        AgentStatus = _try_import("framework.schemas.agent_status").AgentStatus

        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)
        assert result.get("status") == AgentStatus.SUCCESS.value
        output = result.get("output")
        assert isinstance(output, str) and output

    def test_status_is_the_value_string(self):
        agent, ctx = _agent_and_context("pb6-status-test")
        AgentStatus = _try_import("framework.schemas.agent_status").AgentStatus

        result = agent.invoke(_VALID_PAYLOAD, ctx=ctx)
        status = result.get("status")
        # State status is always the `.value` string — a bare enum here is a bug,
        # so there is no enum-tolerant fallback.
        assert type(status) is str  # noqa: E721
        assert status in (AgentStatus.SUCCESS.value, AgentStatus.ERROR.value)


class TestPBOutputBoundary:
    """PB: the output boundary blocks what must never be published."""

    def test_clean_report_passes_the_boundary(self):
        mod = _try_import("src.nodes.post_process_node")
        AgentStatus = _try_import("framework.schemas.agent_status").AgentStatus

        result = mod.PostProcessNode().execute(
            {
                "report_output": "QC Inspection Report\nlength: PASS (capability: PASS)",
                "input_record": _VALID_PAYLOAD,
                "input_context": {},
                "overall_status": "PASS",
            }
        )
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert result.get("formatted_output")

    def test_credential_in_the_report_blocks_publication(self):
        mod = _try_import("src.nodes.post_process_node")
        AgentStatus = _try_import("framework.schemas.agent_status").AgentStatus

        result = mod.PostProcessNode().execute(
            {
                "report_output": "QC Inspection Report. Config: sk-abcdefghijklmnopqrstuvwxyz123456",
                "input_record": _VALID_PAYLOAD,
                "input_context": {},
                "overall_status": "FAIL",
            }
        )
        assert result.get("status") == AgentStatus.ERROR.value
        assert "formatted_output" not in result

    def test_reading_in_the_report_is_redacted(self):
        mod = _try_import("src.nodes.post_process_node")
        AgentStatus = _try_import("framework.schemas.agent_status").AgentStatus

        result = mod.PostProcessNode().execute(
            {
                "report_output": "QC Inspection Report\nlength measured 10.0 mm",
                "input_record": _VALID_PAYLOAD,
                "input_context": {},
                "overall_status": "PASS",
            }
        )
        assert result.get("status") == AgentStatus.SUCCESS.value
        assert "10.0" not in result["formatted_output"]
