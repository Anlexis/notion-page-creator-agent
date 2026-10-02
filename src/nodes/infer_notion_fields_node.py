"""AgentCore Platform v1.0 - inner workflow Step 3: InferNotionFields.

Extracts the page/record title, structured sections, and metadata from the
(redacted) brief and assembles a validated Notion REST API v1 request body for
the classified intent. Human-readable parent hints are resolved to a Notion ID
only when the hint already looks like an ID (v1 limitation - name->ID Search
resolution is a documented v1 follow-up); an unresolved parent is left null
rather than invented (risk mitigation). When an LLM is injected it may enrich
the title; the deterministic core covers title + sections + block/property
assembly so the node runs without a live LLM.
"""

import logging
import math
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

_logger = logging.getLogger("cmn_c2_237.infer_notion_fields")

# A Notion object ID is a 32-hex UUID (with or without dashes).
_NOTION_ID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
# Curly/straight quotes and the full-width colon are written as \u escapes so
# the source stays pure ASCII (push-safe); Python's re parser interprets \uXXXX
# inside these raw patterns.
_TITLE_QUOTED_RE = re.compile(r'titled\s+["“]([^"”]+)["”]', re.IGNORECASE)
_FOR_RE = re.compile(r'\bfor\s+["“]?([^"”\n.]+?)["”]?(?:[.\n]|$)', re.IGNORECASE)
# "Key: value" metadata lines (ASCII or full-width colon). CJK ranges:
# hiragana/katakana ぀-ヿ + CJK unified 一-鿿.
_KV_RE = re.compile(r"^\s*([A-Za-z぀-ヿ一-鿿][\w \-぀-ヿ一-鿿]{0,40})[:：]\s*(.+?)\s*$")

# Notion database property value type signals (best-effort deterministic mapping).
_EMAIL_VALUE_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_URL_VALUE_RE = re.compile(r"^https?://\S+$")
_DATE_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NUMBER_VALUE_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

# Notion's `number` property is an IEEE-754 double. A caller-supplied metadata
# value matching _NUMBER_VALUE_RE can still overflow it: `float("1"*400 + ".5")`
# returns inf, which json.dumps() then emits as the literal `Infinity` — invalid
# JSON that no Notion endpoint accepts, and a value that compares False against
# every bound if a downstream reader ever compares it. Parse through a finite
# check and fail CLOSED to rich_text rather than shipping a non-finite number.
_NUMBER_ABS_MAX = 1e15  # beyond this a double no longer represents integers exactly


def _finite_number(text: str) -> float | int | None:
    """Return a finite int/float for `text`, or None when it is not representable."""
    try:
        parsed = float(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(parsed) or abs(parsed) > _NUMBER_ABS_MAX:
        return None
    if "." in text:
        return parsed
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


class InferNotionFieldsNode(FunctionNode):
    """Extract entities and assemble the Notion REST API v1 request body."""

    # S-1: derives fields from already-validated text; the privileged write
    # happens downstream in CallNotionApiNode (INTERNAL) - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any = None) -> None:
        self._llm = llm

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        intent = state.get("intent", "create_page") or "create_page"
        parent_hint = state.get("parent_hint", "") or ""

        if not text.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["InferNotionFieldsNode: missing validated_input"],
            }

        title = self._infer_title(text)
        if self._llm is not None and title in ("", "Untitled"):
            title = self._enrich_title_via_llm(text, title, state.get("correlation_id", ""))

        metadata, body_lines = self._parse_brief(text)

        # Resolve the parent hint to an ID only when it already looks like a
        # Notion ID; otherwise leave it null (never invent - v1 name resolution
        # is a documented follow-up).
        cleaned_hint = parent_hint.strip()
        parent_id = cleaned_hint if _NOTION_ID_RE.match(cleaned_hint) else ""

        if intent == "create_database_record":
            payload = self._build_db_record(parent_id, title, metadata)
        elif intent == "append_blocks":
            payload = self._build_append(parent_id, metadata, body_lines)
        else:  # create_page (default)
            payload = self._build_page(parent_id, title, metadata, body_lines)

        # S-4: audit the assembled payload shape - field signals only, not content.
        emit_trace_event(
            "notion_fields_inferred",
            {
                "intent": intent,
                "has_parent_id": bool(parent_id),
                "n_metadata": len(metadata),
                "n_body_lines": len(body_lines),
            },
            state,
        )

        return {
            "page_title": title,
            "parent_id": parent_id,
            "notion_payload": to_json(payload),
            "status": AgentStatus.SUCCESS.value,
        }

    # -- extraction -----------------------------------------------------------

    def _infer_title(self, text: str) -> str:
        m = _TITLE_QUOTED_RE.search(text)
        if m:
            return m.group(1).strip()[:200]
        m = _FOR_RE.search(text)
        if m:
            return m.group(1).strip()[:200]
        first = text.strip().splitlines()[0] if text.strip() else ""
        # Trim a leading imperative ("Create a page ...") for a cleaner title.
        first = re.sub(r"^(create|add|append|log|new)\b[^:]{0,40}?:\s*", "", first, flags=re.IGNORECASE)
        return (first or "Untitled").strip()[:200]

    def _enrich_title_via_llm(self, text: str, fallback: str, correlation_id: str = "") -> str:
        try:
            resp = self._llm.complete(
                [{"role": "user", "content": f"Suggest a concise Notion page title (<=12 words) for: {text}"}]
            )
            content = str((resp or {}).get("content", "")).strip()
            return content[:200] if content else fallback
        except Exception as exc:
            _logger.warning(
                "LLM title enrichment failed; using deterministic title " "(correlation_id=%s): %s",
                correlation_id,
                exc,
            )
            return fallback

    def _parse_brief(self, text: str) -> "tuple[list[tuple[str, str]], list[str]]":
        """Return ([(key, value), ...] metadata, [free-text body line, ...])."""
        metadata: list[tuple[str, str]] = []
        body_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            m = _KV_RE.match(stripped)
            if m:
                metadata.append((m.group(1).strip(), m.group(2).strip()))
            else:
                body_lines.append(stripped)
        return metadata, body_lines

    # -- payload assembly -----------------------------------------------------

    def _title_property(self, title: str) -> dict[str, Any]:
        return {"title": [{"type": "text", "text": {"content": title or "Untitled"}}]}

    def _paragraph_block(self, content: str) -> dict[str, Any]:
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": content}}]},
        }

    def _heading_block(self, content: str) -> dict[str, Any]:
        return {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": content}}]},
        }

    def _bulleted_block(self, content: str) -> dict[str, Any]:
        return {
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": content}}]},
        }

    def _build_children(self, metadata: list[tuple[str, str]], body_lines: list[str]) -> list[dict[str, Any]]:
        children: list[dict[str, Any]] = []
        for key, value in metadata:
            children.append(self._heading_block(key))
            children.append(self._paragraph_block(value))
        for line in body_lines:
            bare = re.sub(r"^[-*•\d.)\s]+", "", line)
            if bare != line and bare:  # looked like a list item
                children.append(self._bulleted_block(bare))
            else:
                children.append(self._paragraph_block(line))
        return children

    def _parent_ref(self, parent_id: str, kind: str) -> dict[str, str]:
        # kind: "page_id" | "database_id". Null-safe: empty id -> empty ref
        # (the executor surfaces this as an error rather than writing to the
        # wrong place).
        if not parent_id:
            return {}
        return {kind: parent_id}

    def _build_page(
        self,
        parent_id: str,
        title: str,
        metadata: list[tuple[str, str]],
        body_lines: list[str],
    ) -> dict[str, Any]:
        return {
            "parent": self._parent_ref(parent_id, "page_id"),
            "properties": self._title_property(title),
            "children": self._build_children(metadata, body_lines),
        }

    def _build_db_record(self, parent_id: str, title: str, metadata: list[tuple[str, str]]) -> dict[str, Any]:
        properties: dict[str, Any] = {"Name": self._title_property(title)}
        for key, value in metadata:
            if key.lower() in ("name", "title"):
                continue
            properties[key] = self._map_property(value)
        return {"parent": self._parent_ref(parent_id, "database_id"), "properties": properties}

    def _build_append(self, parent_id: str, metadata: list[tuple[str, str]], body_lines: list[str]) -> dict[str, Any]:
        return {"block_id": parent_id, "children": self._build_children(metadata, body_lines)}

    def _map_property(self, value: str) -> dict[str, Any]:
        v = value.strip()
        if _EMAIL_VALUE_RE.match(v):
            return {"email": v}
        if _URL_VALUE_RE.match(v):
            return {"url": v}
        if _DATE_VALUE_RE.match(v):
            return {"date": {"start": v}}
        if _NUMBER_VALUE_RE.match(v):
            number = _finite_number(v)
            if number is not None:
                return {"number": number}
        # Default: rich_text — also the fail-closed landing for a numeric-looking
        # value that does not parse to a finite number.
        return {"rich_text": [{"type": "text", "text": {"content": v}}]}
