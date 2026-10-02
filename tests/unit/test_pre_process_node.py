# CMN-C2-237 - Unit tests: PreProcessNode (outer backbone)
# Mirrors the sibling ToolCallingAgent's tests/unit/test_pre_process_node.py (Jira -> Notion).

import json

from framework.schemas.agent_status import AgentStatus
from src.nodes.pre_process_node import PreProcessNode


class TestPreProcessNode:
    def setup_method(self):
        self.node = PreProcessNode()

    def test_serializes_request_with_parent_hint(self):
        state = {
            "user_input": "Create a page titled 'Launch Plan'",
            "input_context": {"parent_hint": "Workspace Home"},
            "node_history": [],
            "error_log": [],
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["parent_hint"] == "Workspace Home"
        payload = json.loads(result["validated_input"])
        assert payload["text"] == "Create a page titled 'Launch Plan'"
        assert payload["parent_hint"] == "Workspace Home"

    def test_parent_id_takes_priority(self):
        state = {
            "user_input": "add a row",
            "input_context": {"parent_id": "abc-123", "database_id": "db-9"},
            "node_history": [],
            "error_log": [],
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["parent_hint"] == "abc-123"

    def test_parent_from_notion_url_in_brief(self):
        # Marketplace chat: input_context carries no parent key, so the id the
        # user typed (here as a notion.so URL tail) is the parent target.
        state = {
            "user_input": "Create a page titled 'Q3' under https://www.notion.so/Goals-3eb0e01046d180a5a0bbcbd267d23446",
            "input_context": {"conversation_history": []},
            "node_history": [],
            "error_log": [],
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["parent_hint"] == "3eb0e01046d180a5a0bbcbd267d23446"
        # The reference is consumed as the target, not left in the brief text.
        assert json.loads(result["validated_input"])["text"] == "Create a page titled 'Q3'"

    def test_input_context_parent_wins_over_brief(self):
        state = {
            "user_input": "Create a page under 3eb0e010-46d1-80a5-a0bb-cbd267d23446",
            "input_context": {"parent_id": "ctx-1"},
            "node_history": [],
            "error_log": [],
        }
        assert self.node.execute(state)["parent_hint"] == "ctx-1"

    def test_database_id_fallback(self):
        state = {
            "user_input": "log a record",
            "input_context": {"database_id": "db-1"},
            "node_history": [],
            "error_log": [],
        }
        result = self.node.execute(state)
        assert result["parent_hint"] == "db-1"

    def test_s1_strips_html_markup(self):
        state = {
            "user_input": "Create <script>alert(1)</script>a page",
            "input_context": {},
            "node_history": [],
            "error_log": [],
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        payload = json.loads(result["validated_input"])
        assert "<script>" not in payload["text"]
        assert "</script>" not in payload["text"]

    def test_empty_input_errors(self):
        state = {"user_input": "   ", "input_context": {}, "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_missing_input_errors(self):
        state = {"input_context": {}, "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_status_written_as_plain_string(self):
        # Regression guard: State "status" must be the .value string,
        # never a bare AgentStatus enum member.
        success = self.node.execute(
            {
                "user_input": "Create a page titled 'Launch Plan'",
                "input_context": {},
                "node_history": [],
                "error_log": [],
            }
        )
        # noqa E721 deliberately: AgentStatus is `class AgentStatus(str, Enum)`,
        # so isinstance(AgentStatus.SUCCESS, str) is True — an isinstance check
        # would pass on the exact bug this guard exists to catch.
        assert type(success["status"]) is str  # noqa: E721
        failure = self.node.execute(
            {
                "user_input": "   ",
                "input_context": {},
                "node_history": [],
                "error_log": [],
            }
        )
        assert type(failure["status"]) is str  # noqa: E721
