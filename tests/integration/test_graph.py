# CMN-C2-237 - Integration test: full outer graph compile() + invoke()
# Mirrors the sibling ToolCallingAgent's tests/integration/test_graph.py (Jira -> Notion). This test
# drives the inner NotionWorkflowGraph pipeline end-to-end (validate -> classify
# -> infer -> call -> confirm) through the outer 5-node backbone.
#
# NOTE (real-SDK masking): the framework masks PII/identifiers in node text output
# ([MASKED]) - including the dashed 8-4-4-4-12 UUID form. A resolvable Notion parent
# must therefore be supplied as an UNDASHED 32-hex id (still a valid Notion object
# id, but not the dashed-UUID pattern the masker targets), so it survives to
# InferNotionFieldsNode and the write completes. Write evidence is asserted by
# presence (mask-robust), never by raw value.

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.graph.graph import NotionPageCreatorAgent
from src.services.notion_client import NotionClient

# Undashed 32-hex Notion parent id (valid; survives the dashed-UUID masker).
_PARENT_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"


def _fake_post(url, headers, body):
    return 200, {"object": "page", "id": "page-777", "url": "https://www.notion.so/page777"}


def _build_agent():
    client = NotionClient(post=_fake_post)
    agent = NotionPageCreatorAgent(config={"notion_client": client})
    agent.compile()
    agent.provision_secrets(InMemoryProvider({"NOTION_TOKEN": "mock-token-for-testing"}))
    return agent


def _internal_ctx():
    return InvocationContext(caller_id="ci", caller_trust_level=TrustLevel.INTERNAL)


class TestGraphIntegration:
    def test_full_pipeline_creates_page(self):
        agent = _build_agent()
        with bound_secrets(agent._secrets_provider):
            result = agent.invoke(
                "Create a page for the quarterly launch notes",
                ctx=_internal_ctx(),
                input_context={"parent_id": _PARENT_ID},
            )
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        out = result.get("output", {}) or {}
        assert out.get("intent") == "create_page"
        # FinalizeNode may mask raw identifiers -> assert write evidence surfaces
        # (truthy), not its raw value.
        assert out.get("page_url")
        assert out.get("confirmation")

    def test_outer_backbone_and_graphnode_ran(self):
        agent = _build_agent()
        with bound_secrets(agent._secrets_provider):
            result = agent.invoke(
                "Create a page for the quarterly launch notes",
                ctx=_internal_ctx(),
                input_context={"parent_id": _PARENT_ID},
            )
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        history = result.get("node_history", [])
        # node_history uses class names; the inner 5 steps run inside the GraphNode.
        assert "PreProcessNode" in history
        assert "NotionWorkflowGraphNode" in history
        assert "PostProcessNode" in history

    def test_empty_input_errors(self):
        agent = _build_agent()
        with bound_secrets(agent._secrets_provider):
            result = agent.invoke("   ", ctx=_internal_ctx(), input_context={"parent_id": _PARENT_ID})
        assert result["status"] == AgentStatus.ERROR.value

    def test_raw_pii_not_in_output(self):
        """S-2: an email in the input must not appear verbatim in the response."""
        agent = _build_agent()
        with bound_secrets(agent._secrets_provider):
            result = agent.invoke(
                "Create a page for the launch reported by alice@example.com",
                ctx=_internal_ctx(),
                input_context={"parent_id": _PARENT_ID},
            )
        out = result.get("output", {}) or {}
        assert "alice@example.com" not in str(out)
