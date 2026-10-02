# CMN-C2-237 - Unit tests: CallNotionApiNode (inner Step 4, write side-effect)
# Mirrors the sibling ToolCallingAgent's tests/unit/test_call_jira_api_node.py (Jira -> Notion).

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets.inmemory_provider import InMemoryProvider

from src.nodes.call_notion_api_node import CallNotionApiNode
from src.services.notion_client import NotionClient

_UUID = "11111111-1111-1111-1111-111111111111"


def _fake_post_ok(url, headers, body):
    return 200, {"object": "page", "id": "page-123", "url": "https://www.notion.so/page123"}


def _fake_post_403(url, headers, body):
    return 403, {"message": "restricted by integration permissions"}


def _fake_post_500(url, headers, body):
    return 500, {"code": "internal_server_error"}


def _fake_patch_ok(url, headers, body):
    return 200, {"object": "list", "type": "block", "results": []}


def _provider():
    return InMemoryProvider({"NOTION_TOKEN": "mock-token-for-testing"})


def _state(**kw):
    base = {
        "notion_payload": {
            "parent": {"page_id": _UUID},
            "properties": {"title": [{"type": "text", "text": {"content": "x"}}]},
            "children": [],
        },
        "intent": "create_page",
        "parent_id": _UUID,
        "correlation_id": "c1",
        "session_id": "s1",
        "thread_id": "th1",
        "trace_id": "t1",
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "node_history": [],
        "error_log": [],
    }
    base.update(kw)
    return base


class TestCallNotionApiNode:
    def test_create_page_success(self):
        client = NotionClient(post=_fake_post_ok)
        node = CallNotionApiNode(notion_client=client)
        with bound_secrets(_provider()):
            result = node.execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["page_id"] == "page-123"
        assert result["page_url"] == "https://www.notion.so/page123"

    def test_create_via_default_v1_stub(self):
        # Default transport = deterministic, network-free v1 stub.
        node = CallNotionApiNode(notion_client=NotionClient())
        with bound_secrets(_provider()):
            result = node.execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["page_id"]
        assert result["page_url"].startswith("https://www.notion.so/")

    def test_append_blocks_success(self):
        client = NotionClient(patch=_fake_patch_ok)
        node = CallNotionApiNode(notion_client=client)
        state = _state(
            intent="append_blocks",
            notion_payload={"block_id": _UUID, "children": [{"object": "block"}]},
        )
        with bound_secrets(_provider()):
            result = node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["page_id"] == _UUID
        assert result["page_url"] == "https://www.notion.so/" + _UUID.replace("-", "")

    def test_api_error_4xx_surfaces_status_error(self):
        client = NotionClient(post=_fake_post_403)
        node = CallNotionApiNode(notion_client=client)
        with bound_secrets(_provider()):
            result = node.execute(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("403" in e for e in result["error_log"])

    def test_api_error_5xx_surfaces_status_error(self):
        client = NotionClient(post=_fake_post_500)
        node = CallNotionApiNode(notion_client=client)
        with bound_secrets(_provider()):
            result = node.execute(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("500" in e for e in result["error_log"])

    def test_missing_client_errors(self):
        node = CallNotionApiNode(notion_client=None)
        with bound_secrets(_provider()):
            result = node.execute(_state())
        assert result["status"] == AgentStatus.ERROR.value

    def test_missing_payload_errors(self):
        client = NotionClient(post=_fake_post_ok)
        node = CallNotionApiNode(notion_client=client)
        with bound_secrets(_provider()):
            result = node.execute(_state(notion_payload=None))
        assert result["status"] == AgentStatus.ERROR.value

    def test_unresolved_parent_errors(self):
        client = NotionClient(post=_fake_post_ok)
        node = CallNotionApiNode(notion_client=client)
        state = _state(notion_payload={"parent": {}, "properties": {}}, parent_id="")
        with bound_secrets(_provider()):
            result = node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert any("parent" in e for e in result["error_log"])

    def test_secret_unavailable_errors(self):
        client = NotionClient(post=_fake_post_ok)
        node = CallNotionApiNode(notion_client=client)
        # Provider bound but without NOTION_TOKEN -> ctx.secrets.require raises.
        with bound_secrets(InMemoryProvider({})):
            result = node.execute(_state())
        assert result["status"] == AgentStatus.ERROR.value

    def test_s1_trust_gate_allows_verified_external(self):
        """S-1 (corrected inner-trust): the write is ANONYMOUS, so a real external
        (VERIFIED_EXTERNAL) caller reaches it via BaseNode.__call__ and is NOT denied.

        The single external gate lives on the outer backbone pre_process
        (VERIFIED_EXTERNAL); the inner write runs under the caller's UNELEVATED
        context, so declaring INTERNAL here would S-1-deny a real external caller
        before the write ever runs. See tests/proof_of_boundary/test_pb_invoke_order.py.
        """
        assert CallNotionApiNode.required_trust_level == TrustLevel.ANONYMOUS
        client = NotionClient(post=_fake_post_ok)
        node = CallNotionApiNode(notion_client=client)
        with bound_secrets(_provider()):
            # __call__ runs the S-1 gate; VERIFIED_EXTERNAL >= ANONYMOUS -> allowed.
            result = node(_state(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL.value))
        assert result["status"] == AgentStatus.SUCCESS.value, f"result={result!r}"
        assert result.get("page_id") == "page-123"


class TestDeclaredWriteDeadline:
    """config/config.yaml declares timeout_s; this node is its reader.

    A declared runtime value with no reader is dead configuration — the failure
    mode is silent, because the code degrades to its in-code default and the
    declaration reads as if it were in force. These tests prove the declared
    value changes behaviour.
    """

    def test_declared_timeout_is_enforced(self):
        import time

        def _slow_post(url, headers, body):
            time.sleep(0.5)
            return 200, {"object": "page", "id": "p", "url": "u"}

        node = CallNotionApiNode(notion_client=NotionClient(post=_slow_post), timeout_s=0.05)
        with bound_secrets(_provider()):
            result = node.execute(_state())
        assert result["status"] == AgentStatus.ERROR.value
        assert any("deadline" in e for e in result["error_log"])
        # Closed-set label only: no transport detail, no traceback, no paths.
        joined = " ".join(result["error_log"])
        assert "Traceback" not in joined
        assert "/Users/" not in joined

    def test_a_generous_deadline_lets_the_same_write_through(self):
        """The control — a refuse-everything deadline cannot pass the test above."""
        import time

        def _slow_post(url, headers, body):
            time.sleep(0.05)
            return 200, {"object": "page", "id": "page-123", "url": "https://www.notion.so/page123"}

        node = CallNotionApiNode(notion_client=NotionClient(post=_slow_post), timeout_s=30)
        with bound_secrets(_provider()):
            result = node.execute(_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["page_id"] == "page-123"

    def test_non_finite_or_absent_timeout_fails_closed_to_the_default(self):
        """float('nan')/float('inf') parse fine and then compare False against
        every bound, silently disabling the deadline."""
        from src.nodes.call_notion_api_node import _TIMEOUT_DEFAULT, _finite_timeout

        for bad in (None, "", "abc", float("nan"), float("inf"), -1, 0, 10_000, [30]):
            assert _finite_timeout(bad) == _TIMEOUT_DEFAULT, bad
        assert _finite_timeout(5) == 5.0
        assert _finite_timeout("2.5") == 2.5

    def test_declared_value_reaches_the_node_through_the_graphs(self):
        """End-to-end wiring: config/config.yaml -> outer graph config ->
        _parent_config() -> inner graph -> this node's instance."""
        from src.graph.graph import NotionPageCreatorAgent

        agent = NotionPageCreatorAgent(config={"timeout_s": 7, "notion_client": NotionClient()})
        agent.register_nodes()
        inner = agent._nodes["main"].get_subgraph()
        inner.register_nodes()
        assert inner._nodes["call_notion_api"]._timeout_s == 7.0
