"""AgentCore Platform v1.0 - inner workflow Step 2: ClassifyIntent.

Classifies the (redacted) brief into one of create_page / create_database_record
/ append_blocks. When an LLM is injected (via graph config) it is used;
otherwise a deterministic keyword heuristic runs so the template is testable and
runnable without a live LLM. Low-confidence / unknown falls back to the
"create_page" default with a note.
"""

import logging
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

_logger = logging.getLogger("cmn_c2_237.classify_intent")

_VALID_INTENTS = ("create_page", "create_database_record", "append_blocks")

# Deterministic fallback keyword signals (checked in priority order, most
# specific first).
_KEYWORDS = (
    (
        "append_blocks",
        (
            "append",
            "add a to-do",
            "add an action item",
            "add a block",
            "add a comment",
            "add a note to",
            "追記",
            "ブロックを追加",
            "追加して",
        ),
    ),
    (
        "create_database_record",
        (
            "database",
            "db ",
            " row",
            "record",
            "board",
            "tracker",
            "log a ",
            "log an ",
            "add a task to",
            "crm",
            "new bug report",
            "データベース",
            "レコード",
            "行を追加",
        ),
    ),
    (
        "create_page",
        (
            "create a page",
            "create a meeting",
            "meeting notes",
            "project brief",
            "new page",
            "page for",
            "page titled",
            "ページを作成",
            "ページ作成",
            "作成",
        ),
    ),
)


class ClassifyIntentNode(FunctionNode):
    """Classify the brief into a Notion operation intent."""

    # S-1: read-only classification of already-redacted text - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(self, llm: Any = None) -> None:
        # `llm` is an immutable dependency (a BaseLLM), not mutable per-invocation
        # state - safe to hold on the instance (node statelessness rule).
        self._llm = llm

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        text = state.get("validated_input", "") or ""
        if not text:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ClassifyIntentNode: missing validated_input"],
            }

        intent = self._classify(text, state.get("correlation_id", ""))

        note: list[str] = []
        if intent not in _VALID_INTENTS:
            note = ["ClassifyIntentNode: low-confidence classification, defaulted to create_page"]
            intent = "create_page"

        # S-4: audit the classification decision (LLM call is a side-effect operation).
        emit_trace_event(
            "intent_classified",
            {"intent": intent, "llm_used": self._llm is not None, "defaulted": bool(note)},
            state,
        )

        result = {"intent": intent, "status": AgentStatus.SUCCESS.value}
        if note:
            result["error_log"] = note  # non-fatal note; status stays SUCCESS
        return result

    # -- classification -------------------------------------------------------

    def _classify(self, text: str, correlation_id: str = "") -> str:
        if self._llm is not None:
            return self._classify_via_llm(text, correlation_id)
        return self._classify_via_keywords(text)

    def _classify_via_llm(self, text: str, correlation_id: str = "") -> str:
        prompt = (
            "Classify the following Notion content brief into exactly one intent: "
            "create_page, create_database_record, or append_blocks. Reply with only the intent.\n\n"
            f"{text}"
        )
        try:
            resp = self._llm.complete([{"role": "user", "content": prompt}])
            content = (resp or {}).get("content", "").strip().lower()
        except Exception as exc:
            # Fail soft to the deterministic heuristic - but log so an LLM outage is
            # visible in production (not a silent swallow).
            _logger.warning(
                "LLM classify failed; falling back to keyword heuristic " "(correlation_id=%s): %s",
                correlation_id,
                exc,
            )
            return self._classify_via_keywords(text)
        for intent in _VALID_INTENTS:
            if intent in content:
                return intent
        return self._classify_via_keywords(text)

    def _classify_via_keywords(self, text: str) -> str:
        low = text.lower()
        for intent, words in _KEYWORDS:
            if any(w in low for w in words):
                return intent
        return "create_page"
