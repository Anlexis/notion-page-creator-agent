"""AgentCore Platform v1.0 - outer pre_process node.

Cat 2 outer backbone: serialize the caller's request (raw NL brief + parent
target hint) into a single JSON string in `validated_input`, which the GraphNode
(`main` slot) hands to the inner Notion workflow graph. Business validation
happens inside the inner graph's ValidateInputNode - this node only does the
cheap empty-guard + S-1 sanitize + serialization so the inner graph receives a
well-shaped input.
"""

import json
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.security import sanitize_query, screen_injection

# Upper bound on the caller-supplied write-target hint (a Notion object id is 36
# characters; a human-readable page/database name is comfortably shorter).
_PARENT_HINT_MAX = 256

# A Notion object id (32 hex, optionally dashed as a UUID) typed in the brief -
# bare or as the tail of a notion.so URL. Hex-bounded so a longer hex run
# never yields a truncated id.
_NOTION_ID_IN_TEXT_RE = re.compile(
    r"(?<![0-9a-fA-F])"
    r"([0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"(?![0-9a-fA-F])"
)


class PreProcessNode(FunctionNode):
    """Serialize caller input for the inner workflow graph."""

    # S-1: the outer backbone's SINGLE external trust gate. A real caller enters at
    # VERIFIED_EXTERNAL and the inner Notion write runs under this same (unelevated)
    # context, so the external gate lives HERE, not on the inner write node. An
    # under-trusted (ANONYMOUS) caller is denied at this gate before any write.
    required_trust_level = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only [C1]

        if not user_input or not user_input.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: user_input is empty or missing"],
            }

        # S-2: the template's OWN injection screen, on the node that owns the
        # caller contract. Fails CLOSED, naming the FIELD and a closed-set reason
        # class - never the matched text, which would hand the caller a working
        # oracle for the screen. screen_injection() checks the string both raw
        # and markup-stripped, because the strip changes what is detectable in
        # both directions.
        reason = screen_injection(user_input)
        if reason:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: user_input refused ({reason})"],
            }

        # S-1: strip HTML markup + cap length before JSON serialization.
        sanitized_input = sanitize_query(user_input.strip())

        # Parent target is caller-supplied and never inferred: parent_id /
        # parent_hint / database_id from input_context (any one), else a Notion
        # id/URL the caller typed in the brief itself - the only channel a
        # Marketplace chat has, since its input_context carries no parent key.
        # Bounded: input_context is caller-controlled and, unlike user_input, is
        # not capped by sanitize_query. A Notion object id is 36 chars; the cap
        # leaves room for a human-readable hint without letting an unbounded
        # string ride into State (and into every checkpoint of it).
        parent_hint = str(
            input_context.get("parent_id") or input_context.get("parent_hint") or input_context.get("database_id") or ""
        ).strip()[:_PARENT_HINT_MAX]
        if not parent_hint:
            typed = _NOTION_ID_IN_TEXT_RE.search(sanitized_input)
            if typed:
                parent_hint = typed.group(1)
                # Drop the "under <url/id>" reference so it never lands in the title.
                sanitized_input = re.sub(
                    r"(?:\b(?:under|in|inside|into)\s+)?\S*" + re.escape(parent_hint) + r"\S*",
                    "",
                    sanitized_input,
                    count=1,
                    flags=re.IGNORECASE,
                )
                sanitized_input = re.sub(r"[ \t]{2,}", " ", sanitized_input).strip()

        # The context channel carries caller text too, and the framework's own PII
        # mask covers user_input only — so every screen applied to the brief runs
        # on the hint as well. Anything else leaves the cheaper channel open.
        hint_reason = screen_injection(parent_hint)
        if hint_reason:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"PreProcessNode: input_context parent hint refused ({hint_reason})"],
            }
        parent_hint = sanitize_query(parent_hint, max_length=_PARENT_HINT_MAX)

        validated_input = json.dumps({"text": sanitized_input, "parent_hint": parent_hint})

        # S-4: audit the shaped request - parent-hint presence only, not the raw text.
        emit_trace_event(
            "notion_request_serialized",
            {"has_parent_hint": bool(parent_hint)},
            state,
        )

        return {
            "validated_input": validated_input,
            "parent_hint": parent_hint,
            "status": AgentStatus.SUCCESS.value,
        }
