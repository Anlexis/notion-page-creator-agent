# CMN-C2-237 - Unit tests: InferNotionFieldsNode (inner Step 3)
# Mirrors the sibling ToolCallingAgent's tests/unit/test_infer_jira_fields_node.py (Jira -> Notion).

from framework.schemas.agent_status import AgentStatus
from src.nodes.infer_notion_fields_node import InferNotionFieldsNode
from src.schemas.state import from_json

_UUID = "11111111-1111-1111-1111-111111111111"


class TestInferNotionFieldsNode:
    def setup_method(self):
        self.node = InferNotionFieldsNode()

    def _state(self, text, intent="create_page", parent_hint=_UUID, **kw):
        state = {
            "validated_input": text,
            "intent": intent,
            "parent_hint": parent_hint,
            "node_history": [],
            "error_log": [],
        }
        state.update(kw)
        return state

    def test_create_page_builds_payload(self):
        text = 'Create a page titled "Launch Plan"\nOwner: Alice\nShip the Q3 release notes'
        result = self.node.execute(self._state(text, intent="create_page"))
        assert result["status"] == AgentStatus.SUCCESS.value
        # ADR-005: notion_payload is stored as a JSON string, not a native dict.
        assert isinstance(result["notion_payload"], str)
        fields = from_json(result["notion_payload"], {})
        assert fields["parent"] == {"page_id": _UUID}
        assert fields["properties"]["title"][0]["text"]["content"] == "Launch Plan"
        assert len(fields["children"]) >= 2
        assert result["page_title"] == "Launch Plan"
        assert result["parent_id"] == _UUID

    def test_create_database_record_maps_property_types(self):
        text = "Add a record\nEmail: dev@example.com\nCount: 3\nDue: 2026-08-01"
        result = self.node.execute(self._state(text, intent="create_database_record"))
        assert result["status"] == AgentStatus.SUCCESS.value
        fields = from_json(result["notion_payload"], {})
        assert fields["parent"] == {"database_id": _UUID}
        props = fields["properties"]
        assert "Name" in props
        assert props["Email"] == {"email": "dev@example.com"}
        assert props["Count"] == {"number": 3}
        assert props["Due"] == {"date": {"start": "2026-08-01"}}

    def test_append_blocks_payload(self):
        text = "Append notes\n- item one\n- item two"
        result = self.node.execute(self._state(text, intent="append_blocks"))
        assert result["status"] == AgentStatus.SUCCESS.value
        fields = from_json(result["notion_payload"], {})
        assert fields["block_id"] == _UUID
        assert len(fields["children"]) >= 1

    def test_parent_not_invented_when_hint_absent(self):
        result = self.node.execute(self._state("Create a page for the launch", parent_hint=""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["parent_id"] == ""
        fields = from_json(result["notion_payload"], {})
        assert fields["parent"] == {}

    def test_non_id_hint_left_unresolved(self):
        result = self.node.execute(self._state("Create a page for the launch", parent_hint="My Workspace Home"))
        assert result["parent_id"] == ""
        fields = from_json(result["notion_payload"], {})
        assert fields["parent"] == {}

    def test_missing_input_errors(self):
        result = self.node.execute(self._state("", parent_hint=_UUID))
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]


class TestFiniteNumberProperty:
    """Notion's `number` property is an IEEE-754 double.

    A caller metadata value can match the numeric shape and still overflow it:
    float("1"*400 + ".5") is inf, which json.dumps() emits as the bare literal
    `Infinity` — invalid JSON no Notion endpoint accepts, and a value that
    compares False against every bound if a reader ever compares it. Fail CLOSED
    to rich_text rather than shipping a non-finite number.
    """

    def setup_method(self):
        self.node = InferNotionFieldsNode()

    def _prop(self, value):
        return self.node._map_property(value)

    def test_ordinary_numbers_still_map_to_number(self):
        assert self._prop("42") == {"number": 42}
        assert self._prop("-7") == {"number": -7}
        assert self._prop("0.15") == {"number": 0.15}

    def test_overflowing_decimal_falls_back_to_rich_text(self):
        overflow = "1" * 400 + ".5"
        assert float(overflow) == float("inf")  # the hazard is real
        assert "number" not in self._prop(overflow)
        assert "rich_text" in self._prop(overflow)

    def test_oversized_integer_falls_back_to_rich_text(self):
        assert "number" not in self._prop("9" * 40)

    def test_non_finite_words_never_reach_the_number_property(self):
        for text in ("NaN", "Infinity", "-Infinity", "inf", "nan", "1e999"):
            assert "number" not in self._prop(text), text
