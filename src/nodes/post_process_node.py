"""AgentCore Platform v1.0 - outer post_process node.

Cat 2 outer backbone: finalize the response after the inner Notion workflow graph
has run. The GraphNode.merge_output() maps the inner result into the outer state;
this node shapes the caller-facing `formatted_output`.

S-3: the module-level `_run_s3_domain_gate()` helper (called inline from
execute()) re-verifies that a SUCCESS response always carries write evidence
(page_id/page_url).

WHY AN INLINE HELPER AND NOT `_extra_security_gate_output`: a violation must
return a *clearing delta* (see `_cleared_output_state()`), and the extension hook
cannot produce one - it receives only this node's result dict, never the state,
and a raise inside it is caught by `BaseNode.__call__`, which replaces the whole
delta with a bare `{status, error_log, node_history, execution_time}` partial and
so discards the clearing. Only `execute()` can return the delta that contains the
answer text. (An earlier revision of this docstring claimed the SDK auto-wraps
`_extra_security_gate_*` into the LangGraph chain; that is not true of the
installed wheel - `FunctionNode._security_gate_output` calls the hook directly.)

CONTAINMENT (the reason the gate clears rather than only downgrading):
`AgentBaseGraph.get_output` returns `formatted_output or result` with NO status
check, so an ERROR delta that omits `formatted_output` - or sets it to a falsy
value - re-opens the fallback and ships `state["result"]`, i.e. the very answer
the gate just refused to release. The gate therefore returns a TRUTHY withheld
notice AND blanks every output-bearing field.
"""

from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json

# Every outer-state field that can carry answer text, caller text, or write
# evidence. Kept as an explicit inventory so a new field cannot quietly join the
# state without joining the cleared set - tests/unit/test_post_process_node.py
# asserts this tuple covers everything NotionWorkflowGraphNode.merge_output()
# writes plus the caller fields PreProcessNode writes.
_OUTPUT_BEARING_FIELDS: tuple[str, ...] = (
    "result",
    "confirmation",
    "notion_payload",
    "page_id",
    "page_url",
    "page_title",
    "parent_id",
    "parent_hint",
    "intent",
    "validated_input",
    "redaction_flags",
)

# Closed-set violation labels. A reason is a CONSTANT, never a value read back
# out of the state being withheld - a truthy replacement that carries evidence
# is worse than a falsy one, because it satisfies the `or result` guard while
# still disclosing.
REASON_NO_WRITE_EVIDENCE = "output_gate_no_write_evidence"
REASON_UPSTREAM_ERROR = "upstream_error"


def _cleared_output_state() -> "dict[str, Any]":
    """Blank every output-bearing field.

    Returned as part of the violation delta so the key is PRESENT in the delta,
    not merely absent: LangGraph merges partial deltas, so omitting a key leaves
    the previous value in state and a downstream reader (or a checkpoint) picks
    it straight back up.
    """
    return {field: "" for field in _OUTPUT_BEARING_FIELDS}


def _withheld_output(reason: str) -> "dict[str, Any]":
    """The truthy caller-facing replacement for a withheld answer.

    Truthiness is load-bearing: a falsy `formatted_output` re-activates
    `get_output`'s `formatted_output or result` fallback.
    """
    return {"withheld": True, "reason": reason}


def _run_s3_domain_gate(state: dict[str, Any]) -> dict[str, Any]:
    """S-3 domain check: a SUCCESS response must carry write evidence.

    Blocks the outer backbone from returning a caller-facing "success" with no
    page_id/page_url, which would misrepresent the write outcome to the caller.
    Called inline from execute() on the SUCCESS path. Returns {} when the check
    passes, and a full CONTAINED error delta when it does not.
    """
    output = state.get("formatted_output") or {}
    if not output.get("page_id") and not output.get("page_url"):
        return {
            **_cleared_output_state(),
            "formatted_output": _withheld_output(REASON_NO_WRITE_EVIDENCE),
            "status": AgentStatus.ERROR.value,
            "error_log": ["PostProcessNode S-3 gate: SUCCESS output missing page_id/page_url"],
        }
    return {}


class PostProcessNode(FunctionNode):
    """Format the final agent output."""

    # S-1: read-only formatting of the already-written result - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # S-4: audit the final response shaping - outcome signals only, no payload content.
        emit_trace_event(
            "output_formatted",
            {
                "intent": state.get("intent", ""),
                "has_page_id": bool(state.get("page_id")),
                "errored": state.get("status") == AgentStatus.ERROR.value,
            },
            state,
        )

        # Upstream error. The framework's own route() sends a terminal ERROR
        # status straight from `main` to `finalize`, so this branch is reached
        # only on the HITL APPROVED/CORRECTED route (route() returns
        # "post_process" for those regardless of status). It is CONTAINED
        # anyway: the caller learns the request failed and nothing else. It
        # must not echo page_id/page_url - that is write evidence, and telling
        # a caller being informed of failure that a write happened, and which
        # object it touched, is a disclosure - and it must not echo error_log,
        # which carries framework tracebacks and absolute source paths.
        if state.get("status") == AgentStatus.ERROR.value:
            return {
                **_cleared_output_state(),
                "formatted_output": _withheld_output(REASON_UPSTREAM_ERROR),
                "status": AgentStatus.ERROR.value,
            }

        formatted = {
            "formatted_output": {
                "page_id": state.get("page_id", ""),
                "page_url": state.get("page_url", ""),
                "page_title": state.get("page_title", ""),
                "intent": state.get("intent", ""),
                "confirmation": state.get("confirmation", ""),
                "notion_payload": from_json(state.get("notion_payload"), {}),
            },
            "status": AgentStatus.SUCCESS.value,
        }
        # S-3 inline gate: a SUCCESS shape must carry write evidence; on violation
        # return the CONTAINED error delta (cleared fields + truthy notice).
        gate = _run_s3_domain_gate(formatted)
        if gate:
            return gate
        return formatted
