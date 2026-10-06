"""AgentCore Platform v1.0 — MFG-C2-006 service layer.

The service layer is where domain queries, external API wrappers and data
aggregation belong. It must not carry business logic, routing or credentials:
nodes call this, and this calls the shared integration layer.

MFG-C2-006 evaluates a measurement record that the caller submits, against the
specification in ``config/specs.yaml`` merged with the caller's inspection
profile — every input arrives with the request, so no external backend is
wired in this build. The seam is kept deliberately: a deployment that pulls
part-specific tolerances from a quality management system implements
``fetch()`` here, behind the same node contract, without touching the nodes.
"""

from __future__ import annotations

from typing import Any


class Service:
    """Domain service seam — see the module note above."""

    async def fetch(self, query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Fetch domain data for the given query.

        Not implemented in this build: the pipeline is self-contained. A
        deployment backed by a quality management system implements this
        method; raising keeps the unimplemented contract explicit rather than
        silently returning empty data that would read as a clean inspection.
        """
        raise NotImplementedError("Implement fetch() to query a quality management system.")
