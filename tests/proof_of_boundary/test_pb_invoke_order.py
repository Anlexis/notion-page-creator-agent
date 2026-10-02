# PB-6: Backbone invoke-order + external-trust boundary.
#
# A real outer-graph invoke exercising the fixed 5-node backbone
# (initialize -> pre_process -> main[inner Notion workflow] -> post_process -> finalize).
#
# The SINGLE external trust gate lives on the outer backbone pre_process
# (required_trust_level = VERIFIED_EXTERNAL). GraphNode.execute() passes the caller's
# InvocationContext into the inner subgraph UNCHANGED (no trust elevation), so the
# inner Notion write (CallNotionApiNode) is ANONYMOUS and runs under the caller's
# already-gated context. Two trust levels are asserted deliberately:
#
#   * VERIFIED_EXTERNAL (a real external caller): passes the pre_process gate, so the
#     full backbone runs in order (pre_process -> main -> post_process -> finalize) and
#     the confirmation/page_url SURFACE in result["output"] (status success). This is
#     the deploy-stg first-invoke path the template must satisfy.
#   * ANONYMOUS (an under-trusted caller): ANONYMOUS < VERIFIED_EXTERNAL, so the
#     pre_process gate DENIES the request before the inner write can run -> status=error
#     with no successful write output. (This is the S-1 external boundary; the earlier
#     inner-INTERNAL trap that denied real external callers has been removed.)
#
# NOTE (real-SDK masking): the framework masks the dashed 8-4-4-4-12 UUID pattern in
# node text output ([MASKED]); the parent is supplied as an UNDASHED 32-hex Notion id
# so it survives to InferNotionFieldsNode and the write completes. Write evidence is
# asserted by presence (mask-robust), never by raw value.

import pytest

try:
    from framework.schemas.agent_status import AgentStatus
    from framework.schemas.invocation_context import InvocationContext
    from framework.schemas.trust_level import TrustLevel
    from framework.secrets.context import bound_secrets
    from shared.secrets.inmemory_provider import InMemoryProvider

    from src.graph.graph import NotionPageCreatorAgent
    from src.services.notion_client import NotionClient

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only when the framework wheel is absent
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(_IMPORT_ERROR is not None, reason=f"framework wheel unavailable: {_IMPORT_ERROR}")

# Undashed 32-hex Notion parent id (valid; survives the dashed-UUID masker).
_PARENT_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"


def _fake_post(url, headers, body):
    return 200, {"object": "page", "id": "page-pb6", "url": "https://www.notion.so/pagepb6"}


def _build_agent():
    client = NotionClient(post=_fake_post)
    agent = NotionPageCreatorAgent(config={"notion_client": client})
    agent.compile()
    agent.provision_secrets(InMemoryProvider({"NOTION_TOKEN": "mock-token-for-testing"}))
    return agent


class TestBackboneInvokeOrder:
    def test_verified_external_runs_full_backbone_and_surfaces_write(self):
        """External (VERIFIED_EXTERNAL) caller: full backbone runs in order; page_url surfaces.

        The corrected inner-trust posture: a real external caller passes the single
        pre_process gate (VERIFIED_EXTERNAL) and, because the inner write is ANONYMOUS
        and runs under that same unelevated context, the write completes end-to-end.
        This is exactly the deploy-stg first-invoke path.
        """
        agent = _build_agent()
        with bound_secrets(agent._secrets_provider):
            result = agent.invoke(
                user_input="Create a page for the quarterly launch notes",
                ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
                input_context={"parent_id": _PARENT_ID},
            )
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        assert "trace_id" in result and "correlation_id" in result
        history = result.get("node_history", [])
        # Backbone ran in order: pre_process -> main (inner workflow) -> post_process.
        assert (
            history.index("PreProcessNode")
            < history.index("NotionWorkflowGraphNode")
            < history.index("PostProcessNode")
        )
        # confirmation + write evidence surface in result["output"] (FinalizeNode may
        # mask raw identifiers -> assert presence/truthiness, not the raw value).
        out = result.get("output", {}) or {}
        assert out.get("confirmation")
        assert out.get("page_url")

    def test_under_trusted_caller_is_denied(self):
        """Under-trusted (ANONYMOUS) caller: denied at the pre_process gate; no write output.

        ANONYMOUS < VERIFIED_EXTERNAL, so the single external gate on the outer backbone
        pre_process refuses the request before the inner Notion write can run. The invoke
        surface is well-formed (gated, not crashed) and carries no successful write
        evidence.
        """
        agent = _build_agent()
        with bound_secrets(agent._secrets_provider):
            result = agent.invoke(
                user_input="Create a page for the quarterly launch notes",
                ctx=InvocationContext(caller_id="pb6-anon", caller_trust_level=TrustLevel.ANONYMOUS),
                input_context={"parent_id": _PARENT_ID},
            )
        assert result["status"] == AgentStatus.ERROR.value, f"result={result!r}"
        assert "trace_id" in result and "correlation_id" in result
        # Denied before the write -> no successful write evidence surfaces.
        out = result.get("output", {}) or {}
        assert not out.get("page_url")
        assert not out.get("confirmation")

    def test_empty_input_surfaces_error_not_crash(self):
        agent = _build_agent()
        with bound_secrets(agent._secrets_provider):
            result = agent.invoke(
                user_input="   ",
                ctx=InvocationContext(caller_id="pb6-ext", caller_trust_level=TrustLevel.VERIFIED_EXTERNAL),
                input_context={"parent_id": _PARENT_ID},
            )
        assert result["status"] == AgentStatus.ERROR.value
