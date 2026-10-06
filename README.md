# Quality Inspection Data Validation Agent

AI agent for validating manufacturing quality inspection data, built with Agentic Star.

> **Category**: Cat 2 (domain-specific inspection pipeline)
> **Industry**: Manufacturing
> **Template ID**: MFG-C2-006

## Overview

Validates a manufacturing quality-inspection record against a tolerance specification and
returns an inspection report. A caller submits one or more measured dimension rows for a
produced part; the agent screens the submission, evaluates every dimension against the
approved tolerance bands, computes a process-capability verdict when the submission carries
enough rows, and reports the disposition (pass, conditional or fail), the quality flags and
the remediation each failing dimension needs.

Callers may attach an inspection profile alongside the record — a part family, the inspection
stage, per-dimension tolerance limits and a capability threshold — so the same agent serves
different part families without redeployment. Any limits supplied that way are disclosed in
the report, so an inspection result can never be mistaken for one evaluated against the
approved specification when it was not.

The report carries verdicts and dimension names only: measured values and tolerance limits
are never reproduced in it, and the output boundary enforces that independently of the
report builder, so a report can be circulated without disclosing either the readings or the
specification they were judged against.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails
during graph compile / start-up preflight rather than starting in a partially
working state. This is intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit and boundary tests
config/       agent manifest, runtime parameters, tolerance specification
docs/         design and test documentation
```

See `docs/` for the design document and the test specification.

## Customising

1. Put your own tolerance specification in `config/specs.yaml` — dimension bands, the
   measurements every submission must carry, the record-identification fields, and the
   capability threshold.
2. Adjust `config/config.yaml` for your runtime (retry ceiling, timeout).
3. Review the node implementations under `src/nodes/` — `inspection_contract.py` defines what
   a request may contain, and `post_process_node.py` enforces what a report may disclose.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

