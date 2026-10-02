"""AgentCore Platform v1.0 - inner workflow Step 5: Confirm.

Formats the created/updated Notion object (id + url + title) into a
human-readable confirmation message, surfacing the write target for human
review (risk mitigation).
"""

import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

_VERBS = {
    "create_page": "Created page",
    "create_database_record": "Created database record",
    "append_blocks": "Appended blocks to",
}

# The confirmation is a " - "-delimited record whose fields are read positionally
# ("url=", "id="). page_title is CALLER-DERIVED, so a newline or a " - " inside it
# would let a caller manufacture a field that looks like the agent's own output —
# e.g. a title of `X - url=https://attacker.example` renders a second, forged url
# field. Collapse all whitespace (newlines included) and neutralise the delimiter
# before the title is interpolated. Bounded length is enforced upstream (200).
_WHITESPACE_RUN_RE = re.compile(r"\s+")
_TITLE_MAX = 200


def _render_title(raw: object) -> str:
    """Flatten a caller-derived title to one delimiter-free line."""
    text = _WHITESPACE_RUN_RE.sub(" ", str(raw or "")).strip()
    text = text.replace(" - ", " ").replace("'", "")
    return text[:_TITLE_MAX] or "Untitled"


class ConfirmNode(FunctionNode):
    """Build the human-readable confirmation."""

    # S-1: read-only formatting of already-written data - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        page_id = state.get("page_id", "")
        page_url = state.get("page_url", "")
        page_title = state.get("page_title", "")
        intent = state.get("intent", "create_page")

        if not page_id and not page_url:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ConfirmNode: no page_id/page_url to confirm"],
            }

        verb = _VERBS.get(intent, "Wrote")
        parts = [f"{verb} '{_render_title(page_title)}'"]
        if page_url:
            parts.append(f"url={page_url}")
        if page_id:
            parts.append(f"id={page_id}")
        confirmation = " - ".join(parts)

        # S-4: audit the confirmed write - intent + url presence (no content).
        emit_trace_event(
            "notion_confirmed",
            {"intent": intent, "has_url": bool(page_url)},
            state,
        )

        return {
            "confirmation": confirmation,
            # ADR-005 / ARCH-2318: result crosses the State boundary as a JSON
            # string, never a native dict.
            "result": to_json({"page_id": page_id, "page_url": page_url, "confirmation": confirmation}),
            "status": AgentStatus.SUCCESS.value,
        }
