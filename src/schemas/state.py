"""AgentCore Platform v1.0 - CMN-C2-237 Notion Page Creator Agent state."""

# ADR-005: State must be a flat TypedDict - never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects (and
# nested dict/list containers) are not msgpack-safe. Extend AgentState with
# agent-specific fields only. notion_payload is a dict at the point of use but
# stored in State as a JSON string via to_json/from_json below (ADR-005,
# msgpack-safe serialize-on-write pattern). Do NOT add credentials, secrets, or Pydantic models
# (PB-2 / PB-5). The Notion integration token is NEVER stored here - it is read
# via ctx.secrets.require("NOTION_TOKEN") in CallNotionApiNode.

from __future__ import annotations

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a list/dict State value to a compact JSON string (ADR-005, msgpack-safe).

    Returns None for None so the field stays a true Optional[str].
    """
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def from_json(value: Any, default: Any) -> Any:
    """Deserialize a JSON-string State value back to its list/dict form.

    Tolerant by design: None/empty -> default; an already-native list/dict (e.g. a value
    supplied directly in a unit test) passes through unchanged; a malformed string -> default.
    """
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


class State(AgentState):
    """Notion Page Creator agent state.

    Shared fields (user_input, validated_input, status, session_id,
    node_history, error_log, correlation_id, trace_id, hitl_*, etc.) are
    inherited from AgentState. Only Notion-workflow fields are added below.

    Flat / checkpoint-safe contract (ARCH-2318): every domain field is
    NotRequired (nodes return partial dicts; absent until produced) and holds a
    JSON-safe primitive. Composite values (lists/dicts) cross the State
    boundary ONLY as JSON strings via to_json/from_json - never as native
    containers. The Notion integration token is NEVER stored here (accessed
    via ctx.secrets).
    """

    # Caller-supplied write target hint (parent page / database name or ID).
    # Never inferred; resolution to a Notion ID is best-effort (v1: pass-through
    # when the hint already looks like a Notion ID).
    parent_hint: NotRequired[str]  # human-readable name or Notion ID from the brief
    parent_id: NotRequired[str]  # resolved Notion parent page/database ID

    # ClassifyIntent
    intent: NotRequired[str]  # "create_page" | "create_database_record" | "append_blocks"

    # ValidateInput (S-2 deterministic scan)
    # JSON list of patterns redacted from the text before logging (ADR-005:
    # JSON string, not a native list; (de)serialize via to_json/from_json).
    redaction_flags: NotRequired[Optional[str]]

    # InferNotionFields
    page_title: NotRequired[str]  # inferred page/record title
    # JSON {parent, properties, children} - assembled Notion REST API v1 request
    # body (ADR-005: stored as a JSON string, not a native dict; (de)serialize
    # via to_json/from_json).
    notion_payload: NotRequired[Optional[str]]

    # CallNotionApi
    page_id: NotRequired[str]  # id returned by Notion (created page/record)
    page_url: NotRequired[str]  # url returned by Notion

    # Confirm
    confirmation: NotRequired[str]  # human-readable confirmation message

    # Echo of the inner-workflow result for the outer graph - JSON
    # {page_id, page_url, confirmation} string (ADR-005), never a native dict.
    result: NotRequired[Optional[str]]
