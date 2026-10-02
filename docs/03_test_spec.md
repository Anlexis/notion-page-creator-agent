# Test Specification - CMN-C2-237 Notion Page Creator Agent

## Test Strategy
- Test types: Unit (per node + service + inner graph + config) / Integration (full outer graph) / Proof-of-Boundary.
- Location: `tests/unit/`, `tests/integration/`, `tests/proof_of_boundary/`.
- Mirrors a CoE-passed sibling ToolCallingAgent (Jira Issue Agent) test suite, adapted Jira -> Notion.
- The Notion write is exercised through the deterministic, network-free stub
  transport, injected fake transports, and the live `http.client` transport with
  `HTTPSConnection` patched (`test_live_client_performs_real_http_calls`); the proxy
  tunnel is checked against a local fake proxy (`test_proxy_tunnel_uses_http11_connect`);
  no live Notion call.
- Assertion contract (wheel `agenticstar-agentcore==1.0.3` — the version central CI
  installs): the invoke surface is
  `result["output"]` / `status` / `trace_id` / `correlation_id` / `node_history`
  (never `formatted_output`); status is compared to `AgentStatus.SUCCESS`/`.value`
  (lowercase `success`/`error`); the outer graph is called as
  `invoke(user_input=..., ctx=..., input_context=...)`; FinalizeNode may mask raw
  identifiers, so write evidence is asserted by presence, not raw value.

## Unit Tests (`tests/unit/`)

| TC-ID | Test file | Focus | Expected |
|-------|-----------|-------|----------|
| U-01 | test_validate_input_node.py | S-2 flag-and-redact + empty/short guard + JSON-shaped input | email/token redacted, `redaction_flags=[email,token]`; empty/short -> `status=error` |
| U-02 | test_pre_process_node.py | serialize NL brief + parent hint into `validated_input` (JSON); S-1 HTML strip; parent_id > parent_hint > database_id | parent_hint resolved by priority; `<script>` stripped; empty -> error |
| U-03 | test_classify_intent_node.py | intent = create_page / create_database_record / append_blocks (keyword + LLM + default) | correct intent per keyword; LLM path honoured; empty -> error |
| U-04 | test_infer_notion_fields_node.py | title extraction + Notion REST v1 payload for each intent; DB property-type mapping (email/number/date); parent left null when unresolved | page payload with `parent={page_id}`; DB `properties` typed; append `block_id`; unresolved parent -> `parent={}` |
| U-05 | test_call_notion_api_node.py | write side-effect via injected transport + default v1 stub; 4xx/5xx -> `status=error`; missing client/payload/secret; S-1 ANONYMOUS inner-trust (write is ANONYMOUS; the external gate is on pre_process) | page_id/page_url on success; error surfaced with 403/500; `CallNotionApiNode.required_trust_level == ANONYMOUS` so a VERIFIED_EXTERNAL caller reaches the write (not denied) |
| U-06 | test_confirm_node.py | human-readable confirmation per intent verb; id/url formatting | "Created page/database record/Appended blocks to ..."; missing ids -> error |
| U-07 | test_post_process_node.py | `formatted_output` shaping; refusal paths withhold; S-3 write-evidence gate; cleared-set inventory guard | success shape; every refusal returns a TRUTHY notice AND blanks each output-bearing field (asserted by PRESENCE and emptiness, since an omitted key keeps its old value through the LangGraph merge); S-3 blocks SUCCESS lacking page_id/page_url; a new output-bearing field cannot join state without joining the cleared set |
| U-08 | test_notion_client.py | Notion REST v1 client: create_page / append_blocks / search; Bearer + Notion-Version headers; NotionApiError on non-2xx; default stub shape | correct URLs/headers/body; 400 raises NotionApiError; stub returns documented page shape |
| U-09 | test_config.py | FLAT `config/agent.yaml` manifest + `config/config.yaml` runtime sanity | root-level keys, no `agent:` block, single dotted `class`, requires NOTION_TOKEN; every runtime key has a named reader and reaches the graph config |
| U-10 | test_domain_workflow_graph.py | inner `NotionWorkflowGraph` identity + `get_output` contract | name/state_schema correct; success surfaces page fields; NON-success surfaces `output=None` with the inert fields empty; AST guard: no `get_output` in `src/` carries an `or` fallback |
| U-11 | test_notion_client.py / test_call_notion_api_node.py | declared write deadline (`timeout_s`) | a slow transport past the deadline fails CLOSED with a closed-set label; a generous deadline lets the same write through; non-finite/out-of-range values fall back to the default; the declared value reaches the node through both graphs |
| U-12 | test_infer_notion_fields_node.py | finite-number property mapping | ordinary numbers map to `number`; values that overflow a double (`inf`) or are non-finite fail CLOSED to `rich_text` |
| U-13 | test_confirm_node.py | caller-derived title rendering | a newline or embedded `" - "` in the title cannot manufacture a forged `url=`/`id=` field; an ordinary title renders unchanged |
| U-14 | test_validate_input_node.py | detector parity | every credential class the framework's `detect_credentials()` knows is flagged and redacted; overlapping findings are merged, not spliced; an ordinary brief stays byte-identical |
| U-15 | test_s5_secret_provider_boundary.py | secret-provider boundary | NOTION_TOKEN resolves only through the configured namespaced provider, never the process environment |
| U-16 | test_framework_compliance_tc06_tc07.py | framework gate contract | overriding the default input/output gates raises at class-definition time |

## Integration Tests (`tests/integration/`)

| TC-ID | Test | Input | Expected |
|-------|------|-------|----------|
| I-01 | test_full_pipeline_creates_page | NL "Create a page titled ... " + parent id (VERIFIED_EXTERNAL caller) | `status=success`; `output.intent=create_page`; page_url + confirmation surface |
| I-02 | test_outer_backbone_and_graphnode_ran | NL create + parent id | node_history contains PreProcessNode, NotionWorkflowGraphNode, PostProcessNode |
| I-03 | test_empty_input_errors | blank input | `status=error` |
| I-04 | test_raw_pii_not_in_output | NL with an email | email absent from `result["output"]` (S-2) |

## Proof-of-Boundary Tests (`tests/proof_of_boundary/`)

| PB-ID | Boundary | Test | Expected |
|-------|----------|------|----------|
| PB-4 | Import isolation | test_import_isolation.py | AST scan of `src/`: 0 Level-0 imports |
| PB-2/PB-5 | State serialization | test_state_safety.py | `state.py`: no Pydantic/credential fields |
| PB-6 | Backbone invoke-order + inner-trust | test_pb_invoke_order.py | VERIFIED_EXTERNAL caller drives initialize->pre_process->main->post_process->finalize; the write completes through the ANONYMOUS inner node and confirmation/page_url surface in `result["output"]`; an ANONYMOUS caller is denied at pre_process |
| PB-7 | HITL interrupt propagation | test_pb7_hitl_interrupt_propagation.py | conditional skip stub (config/agent.yaml has no `hitl.enabled: true`) |
| PB-8 | Server boot + caller-auth boundary | test_server_boot.py | the entry point imports and constructs with no token set; the caller-auth token is read through the provider accessor; Bearer elevates to VERIFIED_EXTERNAL |
| PB-9 | Output-envelope containment | test_envelope_containment.py | with a DATA-PATH drift that costs the pipeline its write evidence, the refusal envelope carries a truthy withheld notice and none of the un-gated answer — no page id, no url, no confirmation, no traceback, no source path; a clean-path control proves the same request still answers and that the block happened at the gate (`PostProcessNode` in `node_history`) |
| PB-10 | Caller `input_context` credential screen | test_input_context_screen.py | a credential-shaped value on any key — declared or not, nested in a dict or list — is refused 400 naming the FIELD and never the value; an unsafe field name is masked to a positional label; ordinary domain context still succeeds; the screen's refusal set equals the framework detector's block set exactly |

## Notes
- `src/nodes/main_node.py` and `tests/unit/test_main_node.py` were removed: the
  scaffold `MainNode` was unused after the Wave-1 rewiring (the `main` slot is the
  NotionWorkflowGraphNode) and had no remaining reference once `src/examples/`
  was dropped.
- Sibling ToolCallingAgent counterparts (Jira -> Notion): test_validate_input_node /
  test_pre_process_node / test_classify_issue_type_node -> test_classify_intent_node /
  test_infer_jira_fields_node -> test_infer_notion_fields_node / test_call_jira_api_node
  -> test_call_notion_api_node / test_confirm_issue_node -> test_confirm_node /
  test_post_process_node / test_jira_client -> test_notion_client /
  integration/test_graph.py / proof_of_boundary/*.
