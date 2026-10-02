"""AgentCore Platform v1.0 - inner Notion workflow graph (Cat 2 domain workflow).

Instantiated by NotionWorkflowGraphNode.get_subgraph() in graph.py. Inherits
BaseGraph directly for a fully custom linear topology:

    START -> validate_input -> classify_intent -> infer_notion_fields
          -> call_notion_api -> confirm -> END

Config keys (forwarded from the outer graph via _parent_config()):
    llm            - optional BaseLLM for classify/infer (deterministic core if absent)
    notion_client  - NotionClient instance for the write (required to actually write)
"""

from typing import Any

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode
from src.nodes.classify_intent_node import ClassifyIntentNode
from src.nodes.infer_notion_fields_node import InferNotionFieldsNode
from src.nodes.call_notion_api_node import CallNotionApiNode
from src.nodes.confirm_node import ConfirmNode
from src.schemas.state import State


class NotionWorkflowGraph(BaseGraph):
    """Inner graph: NL -> validate -> classify -> infer -> write -> confirm."""

    @property
    def name(self) -> str:
        return "notion_page_workflow"

    @property
    def state_schema(self) -> type:
        return State

    def _validate_config(self) -> None:
        # No mandatory config: llm is optional (deterministic core), and
        # notion_client absence is handled at CallNotionApiNode.execute() as a
        # graceful status=error rather than a compile-time crash.
        pass

    def register_nodes(self) -> None:
        # No super() - BaseGraph.register_nodes() is abstract. Do NOT register
        # initialize / finalize (outer backbone concern).
        llm = self.config.get("llm")
        notion_client = self.config.get("notion_client")
        self._nodes["validate_input"] = ValidateInputNode()
        self._nodes["classify_intent"] = ClassifyIntentNode(llm=llm)
        self._nodes["infer_notion_fields"] = InferNotionFieldsNode(llm=llm)
        self._nodes["call_notion_api"] = CallNotionApiNode(
            notion_client=notion_client,
            # Declared in config/config.yaml as timeout_s; forwarded flat by the
            # outer NotionWorkflowGraphNode._parent_config(). This is the reader
            # that makes the declared value load-bearing rather than decorative.
            timeout_s=self.config.get("timeout_s"),
        )
        self._nodes["confirm"] = ConfirmNode()

    def add_edges(self) -> None:
        self._sg.add_edge(START, "validate_input")
        self._sg.add_edge("validate_input", "classify_intent")
        self._sg.add_edge("classify_intent", "infer_notion_fields")
        self._sg.add_edge("infer_notion_fields", "call_notion_api")
        self._sg.add_edge("call_notion_api", "confirm")
        self._sg.add_edge("confirm", END)

    def route(self, state: AgentState) -> str:
        # Required by BaseGraph ABC. Linear topology -> never called unless an
        # add_conditional_edges() references it.
        return END if state.get("status") == AgentStatus.ERROR.value else "confirm"

    def get_output(self, state: AgentState) -> dict[str, Any]:
        """Shape the sub_result handed to NotionWorkflowGraphNode.merge_output().

        NO `or` FALLBACK, DELIBERATELY. This method used to return
        ``state.get("result") or state.get("confirmation")``, which recreates
        the framework's own `formatted_output or result` hazard one level down:
        a gated-away (falsy) `result` re-opens the `or` and hands the caller the
        pre-gate confirmation - which carries the page id and url, i.e. the
        evidence that a write happened - under whatever status is set. Surface
        `result` on the gated success path only.

        On any non-success the inert provenance fields go out EMPTY rather than
        read back out of the state being withheld: page_id/page_url/confirmation
        are write evidence, and notion_payload is the assembled caller brief.
        """
        common = {
            "status": state.get("status"),
            "error_log": state.get("error_log", []),
            "trace_id": state.get("trace_id", ""),
            "correlation_id": state.get("correlation_id", ""),
            "node_history": state.get("node_history", []),
        }
        if state.get("status") != AgentStatus.SUCCESS.value:
            return {
                **common,
                "output": None,
                "intent": "",
                "parent_id": "",
                "page_id": "",
                "page_url": "",
                "page_title": "",
                "confirmation": "",
                "notion_payload": "",
                "redaction_flags": None,
            }
        return {
            **common,
            "output": state.get("result"),
            "intent": state.get("intent", ""),
            "parent_id": state.get("parent_id", ""),
            "page_id": state.get("page_id", ""),
            "page_url": state.get("page_url", ""),
            "page_title": state.get("page_title", ""),
            "confirmation": state.get("confirmation", ""),
            "notion_payload": state.get("notion_payload", ""),
            # JSON string per ADR-005/ARCH-2318 (None when validate never ran).
            "redaction_flags": state.get("redaction_flags"),
        }
