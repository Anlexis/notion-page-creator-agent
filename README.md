# CMN-C2-237 — NotionPageCreatorAgent

> **Category**: Cat 2 (multi-step domain workflow — tool-calling)
> **Industry**: CMN (cross-industry)

## Overview

Turns a plain-language content brief into a structured Notion object. Given a request such as
*"Create a page titled 'Q3 Launch Plan' for the release — Owner: Sam, Due: 2026-10-01"*, the agent
classifies the intent (create a page, create a database record, or append blocks to an existing
page), extracts the title, metadata and body, assembles a Notion REST API v1 request body, performs
the write, and returns a confirmation naming the object it created.

The write target is never inferred from the free text. It must be supplied by the caller as
`input_context.parent_id` (or `parent_hint` / `database_id`); an unresolved parent produces a clean
error rather than a page written somewhere unintended.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

### What ships, and what does not

The Notion client (`src/services/notion_client.py`) ships a **deterministic, network-free stub
transport** by default. It returns the documented Notion response shape — a synthetic page `id` and
`url` derived from the request — so the pipeline is runnable and testable without a live Notion
workspace, but **it does not perform a real Notion write**. To go live, inject a real HTTP transport
at construction time; the method contracts and payload shapes are already Notion REST API v1 exact,
so no business-logic change is needed.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |
| A Notion integration token | Provisioned as the secret `NOTION_TOKEN` and read through the framework secret provider. It is never read from the process environment and never stored in agent state. |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, graph compile and the
start-up preflight raise rather than starting in a partially working state. This is intentional —
a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Configuration

| File | Purpose |
|---|---|
| `config/agent.yaml` | Static manifest, read at root level: identity, entry class, entry trust level, required secrets. |
| `config/config.yaml` | Runtime parameters passed to the graph: `max_retry` (retry routing) and `timeout_s` (the deadline enforced on the Notion write). |

Every key in `config/config.yaml` has a named reader in `src/`; the test suite fails if one does
not, so a declared value cannot quietly become decorative.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and operational documentation
```

See `docs/` for the design and the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Inject a live Notion transport in place of the default stub (see *What ships* above).
3. Review the node implementations under `src/nodes/` for domain-specific logic — in particular
   the intent keywords in `classify_intent_node.py` and the property mapping in
   `infer_notion_fields_node.py`.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
