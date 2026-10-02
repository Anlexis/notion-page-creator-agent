# CMN-C2-237 - Unit tests: PostProcessNode (outer backbone) + envelope containment.

from framework.schemas.agent_status import AgentStatus

from src.graph.graph import NotionWorkflowGraphNode
from src.nodes.post_process_node import (
    _OUTPUT_BEARING_FIELDS,
    REASON_NO_WRITE_EVIDENCE,
    REASON_UPSTREAM_ERROR,
    PostProcessNode,
    _run_s3_domain_gate,
)
from src.schemas.state import to_json

# The un-gated inner answer that must never ride out on a refusal envelope.
_RELEASED = to_json(
    {
        "page_id": "p1",
        "page_url": "https://www.notion.so/p1",
        "confirmation": "Created page 'Launch Plan' - url=https://www.notion.so/p1 - id=p1",
    }
)


def _assert_contained(result, reason):
    """A refusal delta must WITHHOLD, not merely downgrade the status.

    Asserts PRESENCE and emptiness of each cleared key, not `not
    result.get(key)`: LangGraph merges partial deltas, so a gate that clears
    nothing returns a delta without the key and `not result.get(key)` is True —
    the assertion passes on the exact defect it looks like it is testing.
    """
    assert result["status"] == AgentStatus.ERROR.value
    for field in _OUTPUT_BEARING_FIELDS:
        assert field in result, f"{field} absent from the delta — old value survives the merge"
        assert result[field] == "", f"{field} not cleared: {result[field]!r}"
    # Truthy replacement: a falsy formatted_output re-activates get_output's
    # `formatted_output or result` fallback, which is the leak itself.
    assert result["formatted_output"], "formatted_output must be TRUTHY"
    assert result["formatted_output"] == {"withheld": True, "reason": reason}
    # Nothing read back out of the state being withheld.
    assert _RELEASED not in str(result)
    assert "notion.so" not in str(result)
    assert "p1" not in str(result["formatted_output"])


class TestPostProcessNode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_success_formats_output(self):
        state = {
            "status": AgentStatus.SUCCESS.value,
            "page_id": "p1",
            "page_url": "https://www.notion.so/p1",
            "page_title": "Launch Plan",
            "intent": "create_page",
            "confirmation": "Created page 'Launch Plan'",
            "notion_payload": to_json({"properties": {}}),
            "node_history": [],
            "error_log": [],
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        out = result["formatted_output"]
        assert out["page_id"] == "p1"
        assert out["page_url"] == "https://www.notion.so/p1"
        assert out["confirmation"] == "Created page 'Launch Plan'"
        assert out["intent"] == "create_page"

    def test_error_status_is_contained_not_echoed(self):
        """Upstream error: preserve the failure AND withhold everything.

        This replaces an assertion that the branch echoed error_log verbatim.
        error_log carries framework tracebacks with absolute source paths, and
        page_id/page_url are write evidence — telling a caller being informed of
        failure that a write happened, and which object it touched, is a
        disclosure.
        """
        state = {
            "status": AgentStatus.ERROR.value,
            "result": _RELEASED,
            "page_id": "p1",
            "page_url": "https://www.notion.so/p1",
            "page_title": "Launch Plan",
            "intent": "create_page",
            "confirmation": "Created page 'Launch Plan'",
            "notion_payload": to_json({"properties": {}}),
            "error_log": [
                "[CallNotionApiNode] boom\nTraceback (most recent call last):\n"
                '  File "/Users/someone/src/x.py", line 3'
            ],
            "node_history": [],
        }
        result = self.node.execute(state)
        _assert_contained(result, REASON_UPSTREAM_ERROR)
        assert "Traceback" not in str(result)
        assert "/Users/" not in str(result)

    def test_error_status_as_string_value_preserved(self):
        """Framework may carry status as the enum .value (string) at the boundary."""
        result = self.node.execute({"status": AgentStatus.ERROR.value, "error_log": ["boom"], "node_history": []})
        assert result["status"] == AgentStatus.ERROR.value

    def test_s3_gate_blocks_success_without_write_evidence(self):
        """S-3: a SUCCESS formatted_output missing page_id/page_url must be blocked."""
        result = _run_s3_domain_gate(
            {
                "status": AgentStatus.SUCCESS.value,
                "formatted_output": {"page_id": "", "page_url": "", "confirmation": "Created page"},
            }
        )
        _assert_contained(result, REASON_NO_WRITE_EVIDENCE)
        assert any("S-3 gate" in e for e in result["error_log"])

    def test_s3_gate_passes_success_with_write_evidence(self):
        state = {
            "status": AgentStatus.SUCCESS.value,
            "formatted_output": {"page_id": "p1", "page_url": "", "confirmation": "Created page 'Launch Plan'"},
        }
        # Passing the gate returns an empty patch (no downgrade).
        assert _run_s3_domain_gate(state) == {}

    def test_execute_withholds_success_without_write_evidence(self):
        """execute() applies the S-3 inline gate AND withholds the answer.

        The old form of this test asserted only status == ERROR, which passes
        just as well on a gate that withholds nothing — the leak channel is
        `state["result"]`, which the delta must overwrite.
        """
        state = {
            "status": AgentStatus.SUCCESS.value,
            "result": _RELEASED,
            "page_id": "",
            "page_url": "",
            "page_title": "Launch Plan",
            "intent": "create_page",
            "confirmation": "Created page 'Launch Plan'",
            "notion_payload": to_json({}),
            "node_history": [],
            "error_log": [],
        }
        result = self.node.execute(state)
        _assert_contained(result, REASON_NO_WRITE_EVIDENCE)
        assert any("S-3 gate" in e for e in result["error_log"])


def test_reason_labels_match_the_e2e_literals():
    """tests/proof_of_boundary/test_envelope_containment.py writes these labels
    as literals so it still COLLECTS against the pre-fix source (the mutant that
    restores the original must FAIL, not error). Pin them so they cannot drift."""
    assert REASON_NO_WRITE_EVIDENCE == "output_gate_no_write_evidence"
    assert REASON_UPSTREAM_ERROR == "upstream_error"


def test_cleared_set_covers_every_output_bearing_field():
    """Inventory guard: a new field cannot join outer state without joining the
    cleared set. Derived from the code, not from a hand-copied list — the keys
    merge_output() actually writes plus the caller fields pre_process writes."""
    written = set(NotionWorkflowGraphNode().merge_output({}, {"output": "x", "status": AgentStatus.SUCCESS.value}))
    # status / error_log are framework channels, not caller-facing answer text:
    # status must survive (it carries the refusal) and error_log accumulates via
    # an operator.add reducer, so it cannot be cleared by a delta at all.
    written -= {"status", "error_log"}
    # Fields PreProcessNode writes that carry caller text.
    written |= {"validated_input", "parent_hint"}
    missing = written - set(_OUTPUT_BEARING_FIELDS)
    assert not missing, f"output-bearing fields absent from the cleared set: {missing}"
