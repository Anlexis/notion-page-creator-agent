# CMN-C2-237 - Unit tests: ClassifyIntentNode (inner Step 2)
# Mirrors the sibling ToolCallingAgent's tests/unit/test_classify_issue_type_node.py (Jira -> Notion).
# Intents: create_page / create_database_record / append_blocks.

from framework.schemas.agent_status import AgentStatus
from src.nodes.classify_intent_node import ClassifyIntentNode


class _FakeLLM:
    def __init__(self, content):
        self._content = content

    def complete(self, messages):
        return {"content": self._content, "tool_calls": [], "model": "fake"}

    def stream(self, messages):  # pragma: no cover - unused
        yield self._content

    def bind_tools(self, tools):  # pragma: no cover - unused
        return self


class TestClassifyIntentNode:
    def test_keyword_create_page(self):
        node = ClassifyIntentNode()
        result = node.execute(
            {"validated_input": "Create a page titled 'Launch Plan' for the team", "node_history": [], "error_log": []}
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["intent"] == "create_page"

    def test_keyword_create_database_record(self):
        node = ClassifyIntentNode()
        result = node.execute(
            {"validated_input": "Add a new row to the projects tracker database", "node_history": [], "error_log": []}
        )
        assert result["intent"] == "create_database_record"

    def test_keyword_append_blocks(self):
        node = ClassifyIntentNode()
        result = node.execute(
            {"validated_input": "Append a summary to the meeting notes page", "node_history": [], "error_log": []}
        )
        assert result["intent"] == "append_blocks"

    def test_default_create_page_when_unknown(self):
        node = ClassifyIntentNode()
        result = node.execute({"validated_input": "please handle the thing", "node_history": [], "error_log": []})
        assert result["intent"] == "create_page"

    def test_llm_path(self):
        node = ClassifyIntentNode(llm=_FakeLLM("append_blocks"))
        result = node.execute({"validated_input": "some free-form content brief", "node_history": [], "error_log": []})
        assert result["intent"] == "append_blocks"

    def test_empty_errors(self):
        node = ClassifyIntentNode()
        result = node.execute({"validated_input": "", "node_history": [], "error_log": []})
        assert result["status"] == AgentStatus.ERROR.value
