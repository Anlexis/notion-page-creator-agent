"""AgentCore Platform v1.0 - CMN-C2-237 outer graph (Cat 2).

Cat 2: fixed 5-node backbone (initialize -> pre_process -> main -> post_process ->
finalize). Domain complexity is encapsulated in NotionWorkflowGraphNode (`main`
slot), which wraps the inner NotionWorkflowGraph (validate -> classify -> infer ->
call -> confirm). add_edges() is NOT overridden - backbone wiring is the
framework's concern.
"""

import pathlib
from typing import Any, ClassVar

from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from src.nodes.pre_process_node import PreProcessNode
from src.nodes.post_process_node import PostProcessNode
from src.schemas.state import State, to_json

# config/config.yaml, resolved from this file: src/graph/graph.py -> repo root.
# NOTE: the RUNTIME parameters live in config/config.yaml, NOT config/agent.yaml.
# config/agent.yaml is the flat static manifest AgentRegistry reads at ROOT level
# and has no `agent:` block and no `config:` block at all - a reader pointed at
# `agent.config` there returns {} on every call, and every declared value goes
# silently dead while the code degrades to its in-code defaults.
_RUNTIME_CONFIG_PATH = pathlib.Path(__file__).resolve().parents[2] / "config" / "config.yaml"

# Runtime keys this template actually consumes. Declared here so an undeclared
# key in config/config.yaml is visible rather than silently ignored, and so the
# forwarded config carries no dead entries.
_RUNTIME_DEFAULTS: dict[str, Any] = {
    "max_retry": 3,  # read by AgentBaseGraph.route() off the OUTER graph config
    "timeout_s": 30,  # read by CallNotionApiNode as the Notion write deadline
}


def runtime_config() -> dict[str, Any]:
    """Return the declared runtime parameters from config/config.yaml.

    Falls back to _RUNTIME_DEFAULTS when the file is unreadable or PyYAML is
    absent: pyyaml is a transitive dependency of the framework wheel and is not
    declared in pyproject, so graph construction must not hard-depend on it.
    """
    merged = dict(_RUNTIME_DEFAULTS)
    try:
        import yaml
    except ImportError:
        return merged
    try:
        raw = yaml.safe_load(_RUNTIME_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return merged
    if isinstance(raw, dict):
        for key in _RUNTIME_DEFAULTS:
            if key in raw:
                merged[key] = raw[key]
    return merged


def _json_str(value: Any) -> str | None:
    """ADR-005/ARCH-2318 merge guard: composite values cross the State boundary
    as JSON strings — pass primitives/None through, serialize native list/dict."""
    if value is None or isinstance(value, str):
        return value
    return to_json(value)


class NotionWorkflowGraphNode(GraphNode):
    """Wraps the inner Notion workflow graph; assigned to the `main` slot."""

    # Fail fast: re-raise inner-graph exceptions as SubgraphError (default).
    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def __init__(self, llm: Any = None, notion_client: Any = None, timeout_s: Any = None) -> None:
        # Immutable deps forwarded to the inner graph - not per-invocation state.
        self._llm = llm
        self._notion_client = notion_client
        self._timeout_s = timeout_s

    def get_subgraph(self) -> Any:
        from src.graph.domain_workflow_graph import NotionWorkflowGraph

        return NotionWorkflowGraph(config=self._parent_config())

    def extract_input(self, state: AgentState) -> str:
        # pre_process serialized the request into validated_input (JSON string).
        value = state.get("validated_input", state.get("user_input", ""))
        return value if isinstance(value, str) else ""

    def merge_output(self, state: AgentState, sub_result: dict[str, Any]) -> dict[str, Any]:
        # Map only the keys this node changes back into the outer state.
        # ARCH-2318: the merged State must be checkpoint-safe — result and
        # redaction_flags are JSON strings (inner nodes already serialize;
        # _json_str guards against a native value from a hand-built sub_result).
        # error_log stays a list[str] — the AgentState framework contract.
        return {
            "result": _json_str(sub_result.get("output")),
            "status": sub_result.get("status"),
            "intent": sub_result.get("intent", ""),
            "parent_id": sub_result.get("parent_id", ""),
            "page_id": sub_result.get("page_id", ""),
            "page_url": sub_result.get("page_url", ""),
            "page_title": sub_result.get("page_title", ""),
            "confirmation": sub_result.get("confirmation", ""),
            "notion_payload": sub_result.get("notion_payload", ""),
            "redaction_flags": _json_str(sub_result.get("redaction_flags")),
            "error_log": sub_result.get("error_log", []),
        }

    def _parent_config(self) -> dict[str, Any]:
        """Build the config dict handed to the inner NotionWorkflowGraph.

        Every entry here has a NAMED READER. An earlier revision forwarded a
        `configurable` block instead; nothing in src/ ever read `configurable`,
        `max_retry` or `timeout_seconds`, so the declared configuration was
        still dead - one layer further down than before. The keys below are
        forwarded FLAT and consumed by NotionWorkflowGraph.register_nodes():
          llm            -> ClassifyIntentNode / InferNotionFieldsNode
          notion_client  -> CallNotionApiNode
          timeout_s      -> CallNotionApiNode (Notion write deadline)
        """
        return {
            "llm": self._llm,
            "notion_client": self._notion_client,
            "timeout_s": self._timeout_s,
        }


class NotionPageCreatorAgent(AgentBaseGraph):
    """CMN-C2-237 outer graph - Notion Page Creator Agent.

    Backbone: initialize -> pre_process -> main -> post_process -> finalize (fixed).
    Domain logic lives in NotionWorkflowGraphNode (`main` slot). The LLM and Notion
    client are supplied via the graph config and forwarded to the inner graph.
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        # Declared runtime parameters (config/config.yaml) form the BASE of the
        # graph config so `max_retry` reaches AgentBaseGraph.route()'s reader and
        # `timeout_s` reaches CallNotionApiNode. An explicitly-passed key wins,
        # so AgentRegistry (which already passes config/config.yaml as
        # Graph(config=...)) and a test's inline config both stay authoritative.
        super().__init__({**runtime_config(), **(config or {})})

    @property
    def name(self) -> str:
        return "cmn_c2_237"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        super().register_nodes()  # injects InitializeNode + FinalizeNode
        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = NotionWorkflowGraphNode(
            llm=self.config.get("llm"),
            notion_client=self.config.get("notion_client"),
            timeout_s=self.config.get("timeout_s"),
        )
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden - backbone wiring belongs to the framework.
