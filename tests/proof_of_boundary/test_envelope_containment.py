# PB: output-envelope containment through the real ASGI /invoke.
#
# AgentBaseGraph.get_output() is `state.get("formatted_output") or state.get("result")`
# with NO status check. Three properties follow, and this module pins all three:
#
#   1. An ERROR delta that OMITS `formatted_output` falls through to `result` — the
#      un-gated inner answer ships inside the error envelope. The shipped S-3 domain
#      gate returned exactly that shape: `{"status": ERROR, "error_log": [...]}`.
#   2. A FALSY `formatted_output` re-activates the same fallback, so the replacement
#      must be truthy.
#   3. LangGraph merges partial deltas, so a key omitted from the delta keeps its old
#      value in state — clearing must be an explicit write, and a test must assert
#      PRESENCE and emptiness rather than `not result.get(field)`.
#
# THE FAULT IS INJECTED ON THE DATA PATH, NEVER ON THE GATE. `_DriftedGraphNode`
# reproduces a plausible refactor drift in merge_output (the page-evidence keys stop
# being mapped into outer state) so the pipeline hands the gate a SUCCESS shape it
# must refuse, with `state["result"]` already loaded. Patching the gate to force a
# violation would only test the patch.

import pytest

try:
    from framework.schemas.agent_status import AgentStatus

    from src.graph.graph import NotionPageCreatorAgent, NotionWorkflowGraphNode, _json_str
    from src.services.notion_client import NotionClient

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only when the framework wheel is absent
    _IMPORT_ERROR = exc

# Written as a LITERAL, not imported from src.nodes.post_process_node, so this
# module still COLLECTS against the pre-fix source. That matters: the mutant that
# restores the original shipped code is the one that proves this E2E is
# load-bearing, and a module that cannot import against the original produces a
# pytest collection ERROR — which a naive "did any test fail?" check reads as no
# failures and reports the mutant as passing. tests/unit/test_post_process_node.py
# pins the constant against this literal so the two cannot drift.
_REASON_NO_WRITE_EVIDENCE = "output_gate_no_write_evidence"

pytestmark = pytest.mark.skipif(_IMPORT_ERROR is not None, reason=f"framework wheel unavailable: {_IMPORT_ERROR}")

_PARENT_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
_TOKEN = "containment-probe-token"
_PAGE_ID = "page-contain-1"
_PAGE_URL = "https://www.notion.so/pagecontain1"


def _fake_post(url, headers, body):
    return 200, {"object": "page", "id": _PAGE_ID, "url": _PAGE_URL}


class _DriftedGraphNode(NotionWorkflowGraphNode):
    """DATA-PATH fault: merge_output stops mapping the page-evidence keys."""

    def merge_output(self, state, sub_result: dict) -> dict:
        merged = super().merge_output(state, sub_result)
        merged["page_id"] = ""
        merged["page_url"] = ""
        return merged


class _DriftedAgent(NotionPageCreatorAgent):
    def register_nodes(self) -> None:
        super().register_nodes()
        self._nodes["main"] = _DriftedGraphNode(
            llm=self.config.get("llm"),
            notion_client=self.config.get("notion_client"),
            timeout_s=self.config.get("timeout_s"),
        )


def _client(monkeypatch, agent_cls):
    """Build the real ASGI app around `agent_cls` and return a TestClient."""
    starlette_testclient = pytest.importorskip("starlette.testclient")
    import importlib

    from shared.secrets.inmemory_provider import InMemoryProvider

    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    srv = importlib.reload(importlib.import_module("src.api.server"))
    agent = agent_cls(config={"notion_client": NotionClient(post=_fake_post)})
    agent.compile()
    agent.provision_secrets(InMemoryProvider({"NOTION_TOKEN": "mock-token-for-testing"}))
    srv.agent = agent
    return starlette_testclient.TestClient(srv.app)


def _invoke(client):
    return client.post(
        "/invoke",
        json={
            "input": 'Create a page titled "Launch Plan" for the release',
            "session_id": "containment",
            "input_context": {"parent_id": _PARENT_ID},
        },
        headers={"authorization": f"Bearer {_TOKEN}"},
    ).json()


def test_clean_path_control_still_answers(monkeypatch):
    """A refuse-everything gate must not be able to pass this module.

    Also pins that the block in the sibling test happens AT the gate: the same
    request reaches PostProcessNode and produces the real answer.
    """
    envelope = _invoke(_client(monkeypatch, NotionPageCreatorAgent))
    assert envelope["status"] == AgentStatus.SUCCESS.value
    assert "PostProcessNode" in envelope["node_history"]
    assert envelope["output"]["page_url"] == _PAGE_URL
    assert _PAGE_ID in envelope["output"]["confirmation"]


def test_gate_violation_envelope_releases_nothing(monkeypatch):
    """The refusal envelope must carry no released text, no write evidence."""
    envelope = _invoke(_client(monkeypatch, _DriftedAgent))

    assert envelope["status"] == AgentStatus.ERROR.value
    # The block happened at the gate, not upstream.
    assert "PostProcessNode" in envelope["node_history"]

    output = envelope["output"]
    # Truthy replacement — a falsy one re-opens `formatted_output or result`.
    assert output, "envelope output is falsy: the get_output fallback is re-opened"
    assert output == {"withheld": True, "reason": _REASON_NO_WRITE_EVIDENCE}

    # Nothing from the un-gated inner answer, in any representation.
    blob = repr(envelope)
    for released in (_PAGE_ID, _PAGE_URL, "Launch Plan", "Created page", "notion.so"):
        assert released not in blob, f"{released!r} leaked into the error envelope"
    # No traceback, no absolute source path.
    assert "Traceback" not in blob
    assert "/Users/" not in blob
    assert "site-packages" not in blob


def test_result_is_the_fallback_channel_and_is_cleared(monkeypatch):
    """Pin the mechanism, not just the outcome.

    `result` is what `get_output` falls back to. Prove it is loaded on the clean
    path (so the channel is real, not vacuous) and cleared on the refusal path.
    """
    seen: dict = {}
    original = NotionWorkflowGraphNode.merge_output

    def spy(self, state, sub_result):
        merged = original(self, state, sub_result)
        seen["result_before_gate"] = merged.get("result")
        return merged

    monkeypatch.setattr(NotionWorkflowGraphNode, "merge_output", spy)
    envelope = _invoke(_client(monkeypatch, _DriftedAgent))

    # The fallback channel really was loaded when the gate fired — this is not a
    # leak that is contained only because nothing ever writes state["result"].
    assert seen["result_before_gate"]
    assert _PAGE_ID in seen["result_before_gate"]
    # ...and none of it reached the caller.
    assert _PAGE_ID not in repr(envelope)


def test_json_str_guard_is_still_exercised():
    """_json_str keeps composite values msgpack-safe across the State boundary."""
    assert _json_str(None) is None
    assert _json_str("already a string") == "already a string"
    assert _json_str({"a": 1}) == '{"a":1}'
