# Template Design Specification — CMN-C2-237 Notion Page Creator Agent

## Position in AgentCore Architecture

- **Agent Class**: `NotionPageCreatorAgent` (`src/graph/graph.py`)
- **L1 Base**: `AgentBaseGraph`
- **Category**: Cat 2 (multi-step domain workflow, ToolCallingAgent). Outer
  `AgentBaseGraph` 5-node backbone; the domain pipeline is encapsulated in a
  `GraphNode` (`main` slot) wrapping an inner `BaseGraph`
  (`src/graph/domain_workflow_graph.py`).
- **Level 2 Type**: ToolCallingAgent — classify intent -> extract entities ->
  build a Notion REST API v1 payload -> call the tool -> format the response. No
  RAG retrieval, no autonomous ReAct loop.
- **Three-Layer Separation**:
  - State: flat TypedDict `State(AgentState)` (no Pydantic — msgpack incompatible)
  - Node: L1 inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` + `super().register_nodes()`; `add_edges()`
    not overridden on the outer graph)

## Architecture Overview

### Outer graph — node configuration (`src/graph/graph.py`)

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | framework setup (schema, session, trust) | user_input | session/trust fields | InitializeNode (default) |
| pre_process | serialize NL brief + parent hint into `validated_input` (JSON); S-1 HTML/length sanitize | user_input, input_context | validated_input, parent_hint | PreProcessNode (FunctionNode) |
| main | run the inner Notion workflow subgraph | validated_input | result, intent, page_id, page_url, page_title, confirmation, notion_payload | NotionWorkflowGraphNode (GraphNode) |
| post_process | shape caller-facing `formatted_output`; S-3 write-evidence gate | inner-result fields | formatted_output | PostProcessNode (FunctionNode) |
| finalize | framework finalize (metadata, timing) | — | response_metadata | FinalizeNode (default) |

### Inner workflow — node configuration (`src/graph/domain_workflow_graph.py`)

Inner graph inherits `BaseGraph` (fully custom linear topology). The 5 pipeline
steps map 1:1 to inner nodes:

| Inner node | Step | Responsibility | Output |
|------|------|---------------|--------|
| validate_input | 1 ValidateInput | empty/non-brief guard; deterministic (regex) S-2 flag-and-redact of email/token before logging | validated_input, redaction_flags |
| classify_intent | 2 ClassifyIntent | LLM (or deterministic keyword) -> create_page / create_database_record / append_blocks; low-confidence -> create_page | intent |
| infer_notion_fields | 3 InferNotionFields | extract title/sections/metadata; map to Notion blocks (heading_2/paragraph/bulleted_list_item) and DB property types (title/rich_text/select/date/email/number/url); assemble REST v1 payload; unresolved parent left null (not invented) | page_title, parent_id, notion_payload |
| call_notion_api | 4 CallNotionApi | POST /v1/pages (page/DB record) or PATCH /v1/blocks/{id}/children (append); runs under S-1 ANONYMOUS inner-trust (the single external gate is on backbone pre_process); S-3 token via ctx.secrets; S-4 emit_trace_event; 4xx/5xx -> status=error | page_id, page_url |
| confirm | 5 Confirm | format id + url + title into a human-readable confirmation | confirmation, result |

### Data Flow

```
Outer:  START -> initialize -> pre_process -> main -> {route} -> post_process -> finalize -> END
                                              | (RETRY, max 3) ^
Inner (inside main / NotionWorkflowGraphNode):
        START -> validate_input -> classify_intent -> infer_notion_fields
              -> call_notion_api -> confirm -> END
```

### State Definition (`src/schemas/state.py`)

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| parent_hint | str | caller-supplied parent page/database name or ID; never inferred | on create/append |
| parent_id | str | resolved Notion parent ID (v1: pass-through when the hint is already an ID) | when resolvable |
| intent | str | create_page \| create_database_record \| append_blocks | yes |
| redaction_flags | list | S-2 patterns redacted before logging | yes |
| page_title | str | inferred page/record title | yes |
| notion_payload | Optional[str] (JSON) | assembled Notion REST v1 body; ADR-005 msgpack-safe — stored as a JSON string, (de)serialized via `to_json`/`from_json` | yes |
| page_id | str | id returned by Notion | yes |
| page_url | str | url returned by Notion | yes |
| confirmation | str | human-readable confirmation | yes |
| result | Any | inner-workflow result echo | yes |

**State Constraints (mandatory, satisfied):**
- Flat TypedDict only (primitives + JSON-serializable) — no Pydantic/dataclass.
- No JWT / API keys / credentials in State — `NOTION_TOKEN` accessed via `ctx.secrets`.
- InvocationContext read via `InvocationContext.from_state(state)`, never stored in State.

## Security Design (S-1 ... S-4)

- **S-1 Trust gate** — the single external trust gate is on the outer backbone
  `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; every inner
  domain node — **including the write `CallNotionApiNode`** — declares
  `TrustLevel.ANONYMOUS`. `GraphNode.execute()` forwards the caller's
  `InvocationContext` into the subgraph **unchanged** (no elevation), and
  `VERIFIED_EXTERNAL (1) < INTERNAL (2)`, so declaring the inner write `INTERNAL`
  would S-1-deny a legitimate external caller before the write runs — the boundary
  is therefore enforced exactly once, at `pre_process`. Agent-level default trust
  `VERIFIED_EXTERNAL` is declared in `config/agent.yaml`. `src/api/server.py`
  enforces the standalone entry-point Bearer-token auth boundary.
- **S-2 Input flag-and-redact** — `ValidateInputNode.execute()` runs a
  deterministic (regex, NOT LLM) scan for email addresses, the Notion-specific
  integration-token prefixes (`secret_...`, `ntn_...`), and every credential shape
  the framework's own `detect_credentials()` knows, and redacts them before any
  logging. The framework detector is called alongside the local prefixes rather
  than instead of them, so this node's flag set is a SUPERSET of the framework
  gate's block set: a local set narrower than the framework's is a bypass, because
  a value this node misses makes the framework raise inside a later node and the
  wrapper then discards that node's whole delta. Notion briefs may carry internal names but not statutorily-
  regulated PII, so this is flag-and-redact for safe logging, not a hard reject.
  The only deterministic auto-reject is the empty/non-brief guard.
- **S-3 Secrets + output gate** — the integration token is read via
  `ctx.secrets.require("NOTION_TOKEN")` (`InvocationContext.from_state(state)`),
  never `os.environ`, never stored in State; declared under
  `config/agent.yaml requires.secrets`. The module-level `_run_s3_domain_gate()`
  in `src/nodes/post_process_node.py`, called inline from `PostProcessNode.execute()`,
  blocks any SUCCESS response that lacks write evidence (page_id/page_url).
  It is deliberately NOT the `_extra_security_gate_output()` extension hook: a
  violation must return a *clearing delta*, and the hook receives only the node's
  result dict, never the state — and a raise inside it is caught by
  `BaseNode.__call__`, which replaces the whole delta (clearing included) with a
  bare `{status, error_log, node_history, execution_time}` partial.
- **Output-envelope containment** — `AgentBaseGraph.get_output()` returns
  `formatted_output or result` with no status check, so an ERROR delta that omits
  `formatted_output`, or sets it to a falsy value, ships `state["result"]`: the
  un-gated inner answer, inside the error envelope. Every refusal path therefore
  returns a TRUTHY withheld notice AND explicitly blanks every output-bearing
  field (`_OUTPUT_BEARING_FIELDS`). Clearing is an explicit write, not an
  omission: LangGraph merges partial deltas, so a key left out of the delta keeps
  its previous value in state. The inner `NotionWorkflowGraph.get_output()` carries
  no `or` fallback for the same reason — a template can recreate the identical
  hazard one level down in its own subgraph.
- **Caller `input_context` credential screen** — `src/api/server.py` screens
  `input_context` with the framework's own `detect_credentials_in_value()` BEFORE
  `agent.invoke()` and returns 400 naming the field. The framework's
  `InitializeNode` returns `input_context` verbatim into its own result and the
  output gate scans every value of every result, so a credential-shaped value
  anywhere in `input_context` — including on a key this template never declared —
  fails the FIRST node with an opaque error before any template code runs. The
  request cannot succeed either way; the screen makes the failure actionable.
  The framework detector is used rather than a local pattern set so the refusal
  set and the gate's block set cannot drift apart.
- **S-4 Audit** — `emit_trace_event()` is emitted at each pipeline step and on
  the Notion write side-effect (intent + parent-presence signals only, never the
  brief content or credentials). `__call__()` is never overridden; `_invoke_impl`
  is never defined on any node.

## v1 Limitation — Notion client (documented)

`src/services/notion_client.py` mirrors a sibling ToolCallingAgent's API-client
service shape (injectable transport, `NotionApiError`, per-call token, no framework
imports). `NotionClient.live()` wires a stdlib `http.client` transport that
performs real Notion REST API v1 calls; with no transport injected, a
deterministic network-free stub returns the documented response shape (synthetic
`id` + `url`, marked `_stub: True`) for tests and the STG mock deploy only.

## Entry points and the Notion client

Both entry points inject `NotionClient.live()`:

| Entry point | Where | Notes |
|---|---|---|
| Marketplace one-shot Pod | `cli.py` → `extend_config["notion_client"]` | `extend_config` is a Python dict merged over `config/config.yaml`, so a constructed object rides there. |
| Standalone HTTP | `src/api/server.py` → `_notion_client()` | The stub is used only under `STG_MOCK_MODE=true`. |

The client holds no credential: `CallNotionApiNode` reads `NOTION_TOKEN` per call via
`ctx.secrets.require()`, so constructing the client never depends on when secrets are
provisioned. The Pod's outbound call goes through the Envoy egress sidecar named by
`HTTPS_PROXY`. The transport opens that tunnel itself with `CONNECT ... HTTP/1.1`: Python
3.11's `http.client`/`urllib` tunnel sends `HTTP/1.0`, which the sidecar rejects with
426 (`DPE`).

**Parent target on Marketplace.** A Marketplace chat's `input_context` carries only
`conversation_history`, so `PreProcessNode` falls back to a Notion page/database id (or a
`notion.so` URL ending in one) typed in the brief, e.g. `... under
https://www.notion.so/<page-id>`. The reference is removed from the brief text before
title inference. The target page must be shared with the integration that owns
`NOTION_TOKEN`; otherwise Notion returns 404 and the agent reports it as `status=error`.

## Framework Utilization

### Shared Components Used
- [x] InvocationContext — read in `CallNotionApiNode` via `InvocationContext.from_state(state)` (secrets + trust)
- [x] S-1 trust gate — single external gate `PreProcessNode.required_trust_level = TrustLevel.VERIFIED_EXTERNAL`; inner domain nodes (incl. `CallNotionApiNode`) declare `TrustLevel.ANONYMOUS` (caller `InvocationContext` forwarded unchanged into the subgraph)
- [x] S-3 secrets — `ctx.secrets.require("NOTION_TOKEN")`; entry-point `bound_secrets` / `secrets_factory` / `provision_secrets` in `src/api/server.py`; declared in `config/agent.yaml requires.secrets`
- [x] S-4 `emit_trace_event()` — emitted per step + on the Notion write (side-effect); framework lifecycle events NOT re-emitted

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — Cat 2 outer/inner split.
- **Composition target**: inner `NotionWorkflowGraph` (`BaseGraph`) via `NotionWorkflowGraphNode.get_subgraph()`.
- **Config forwarding**: runtime parameters live in `config/config.yaml` (the
  FLAT `config/agent.yaml` manifest has no `agent:` block and no `config:` block,
  so a reader pointed at `agent.config` returns `{}` on every call and every
  declared value goes silently dead). `runtime_config()` loads the file;
  `NotionPageCreatorAgent.__init__` merges it under any explicitly-passed key, so
  `max_retry` reaches `AgentBaseGraph.route()`'s reader and `timeout_s` is
  forwarded flat by `NotionWorkflowGraphNode._parent_config()` — together with
  `llm` and `notion_client` — into the subgraph, where
  `NotionWorkflowGraph.register_nodes()` hands it to `CallNotionApiNode` as the
  Notion write deadline. Every forwarded key has a NAMED READER; a key with no
  reader is dead configuration and `tests/unit/test_config.py` fails on one.
- **Error propagation strategy**: `propagate` (default) — inner errors re-raised as
  `SubgraphError`; per-step `status=error` + `error_log` for API/validation failures
  (no silent pass).

## Output Invariants

- **Monetary precision grid: NOT APPLICABLE.** This template renders no monetary
  aggregates and no computed metrics — its caller-facing output is a Notion object
  id, url, title, intent and confirmation string. There is no rounding rule to
  enforce, so no numeric snap-to-grid gate is installed; installing one would
  corrupt the identifiers this template does render (a snap grammar reads any
  standalone 3-letter uppercase word as a currency marker).
- **The invariant this template enforces instead**: a SUCCESS response must carry
  write evidence (`page_id` or `page_url`). A success shape without it would
  misrepresent the write outcome, so `_run_s3_domain_gate()` downgrades it to a
  contained ERROR.
- **Caller-derived text in a rendered record**: the confirmation is a `" - "`
  delimited record whose fields are read positionally (`url=`, `id=`). The title
  is caller-derived, so `ConfirmNode._render_title()` collapses whitespace
  (newlines included) and neutralises the delimiter before interpolation — a
  caller cannot manufacture a field that reads as the agent's own output.
- **Caller-supplied numbers**: a database-record metadata value that matches the
  numeric shape is parsed through a finite, bounded check before it becomes a
  Notion `number` property. `float("1"*400 + ".5")` is `inf`, which `json.dumps`
  emits as the bare literal `Infinity` — invalid JSON, and a value that compares
  False against every bound. Non-finite and out-of-range values fail CLOSED to
  `rich_text`. The declared `timeout_s` is parsed the same way.

## Import Isolation Confirmation
- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: `framework/` and `shared/` only; `src/services/notion_client.py`
      and `src/services/security.py` have no framework imports (pure service layer)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Fixed multi-step pipeline (Cat 2), not an autonomous loop |
| Composition pattern | flat Cat 1 (MainNode) | GraphNode + inner subgraph | GraphNode + inner subgraph | Cat 2 must not be flat — `gate-composition`; 5 domain steps live in the inner graph |
| LLM dependency | hard-import LLM client | injectable LLM + deterministic core | injectable + deterministic core | template runs/tests without a live LLM; LLM is optional enrichment; no unapproved dep pinned |
| Notion client | live `requests` call | injectable transport; `NotionClient.live()` stdlib `http.client` | injectable; entry points inject `live()`, stub for tests/STG mock | stdlib keeps the service import-isolated (PB-4) and dependency-free |
| Parent target | infer from NL | caller-supplied parent_id/parent_hint | caller-supplied | never write to the wrong location; unresolved hint left null, not invented |
