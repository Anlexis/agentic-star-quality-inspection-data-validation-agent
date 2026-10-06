"""AgentCore Platform v1.0 — MFG-C2-006 agent graph.

Flat Category 2 composition on the fixed backbone:

    START -> initialize -> pre_process -> main -> {route} -> post_process
          -> finalize -> END

``initialize`` and ``finalize`` are injected by ``AgentBaseGraph``. The three
open slots hold plain ``FunctionNode`` subclasses:

    pre_process   PreProcessNode             caller trust gate, record
                                             screening, inspection-profile
                                             validation
    main          QCInspectionPipelineNode   parse -> validate -> flag -> report
    post_process  PostProcessNode            output gate and formatting

There is no inner graph and no ``GraphNode``: the domain work is a linear
sequence, so it lives inside the main-slot node rather than in a subgraph.
``add_edges()`` is not overridden — the standard backbone wiring applies, with
routing (including retry up to the configured ``max_retry``) owned by
``AgentBaseGraph``.
"""

from __future__ import annotations

from framework.graph.agent_base_graph import AgentBaseGraph

from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.qc_inspection_pipeline_node import QCInspectionPipelineNode
from src.schemas.state import State


class MfgC2006Agent(AgentBaseGraph):
    """MFG-C2-006 quality-inspection tolerance validation agent.

    Takes a measurement record for a produced part, evaluates every dimension
    against the effective tolerance rules, and returns an inspection report
    carrying verdicts only.
    """

    @property
    def name(self) -> str:
        return "MFG-C2-006"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        # super() injects InitializeNode (session_id, schema_version, trust
        # level) and FinalizeNode (response metadata, timings).
        super().register_nodes()

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = QCInspectionPipelineNode()
        self._nodes["post_process"] = PostProcessNode()
