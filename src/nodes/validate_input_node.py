"""AgentCore Platform v1.0 - inner workflow Step 1: ValidateInput.

Rejects empty / non-brief input and runs a deterministic (regex, NOT LLM) S-2
scan of the inbound text for email addresses / integration-token-like strings,
which are flag-and-redacted before anything is logged. A Notion content brief
may contain internal page/database names but not statutorily-regulated PII, so
this is flag-and-redact for safe logging, not a hard reject. The only
deterministic auto-reject is the empty / non-brief input guard.
"""

import json
import re
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import to_json

# Deterministic S-2 patterns. Email addresses are domain-local.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

# Notion-SPECIFIC integration-token prefixes the framework detector does not
# know. This set is deliberately an ADDITION to the framework's, never a
# replacement: a local set narrower than the framework's is a bypass, because a
# value the framework catches and this misses makes the framework raise inside a
# later node and the wrapper then discards that node's whole delta.
# The framework's own detect_credentials() covers sk_live_/sk_test_, sk-, eyJ
# (JWT), AKIA, Bearer and db URIs; it is called alongside this, not instead.
_NOTION_TOKEN_RE = re.compile(r"\b(?:secret_[A-Za-z0-9]{6,}|ntn_[A-Za-z0-9]{6,})\b")
_REDACTION = "[REDACTED]"

# Minimum signal that the text is a real brief rather than noise.
_MIN_LEN = 3


def _redact_spans(text: str, findings: list[dict[str, Any]]) -> str:
    """Replace every finding span with _REDACTION, merging overlaps.

    Framework patterns can overlap (``Bearer eyJ...`` matches both bearer_token
    and jwt); replacing overlapping spans independently would splice the middle
    of an already-replaced region back into the text, so the spans are merged
    first and then applied right to left.
    """
    spans = sorted(
        (f["start"], f["end"])
        for f in findings
        if isinstance(f.get("start"), int) and isinstance(f.get("end"), int) and 0 <= f["start"] < f["end"] <= len(text)
    )
    merged: "list[list[int]]" = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    for start, end in reversed(merged):
        text = text[:start] + _REDACTION + text[end:]
    return text


def _scan_and_redact(text: str, flags: list[str]) -> str:
    """Run the full S-2 scan over one caller-supplied string.

    Appends each class it finds to `flags` (deduplicated by the caller's shared
    list) and returns the redacted text. Framework parity is the point of the
    third pass: a local set narrower than the framework's is a bypass, because a
    value this node misses makes the framework raise inside a later node and the
    wrapper then discards that node's whole delta.
    """
    redacted = text
    if _EMAIL_RE.search(redacted):
        if "email" not in flags:
            flags.append("email")
        redacted = _EMAIL_RE.sub(_REDACTION, redacted)
    if _NOTION_TOKEN_RE.search(redacted):
        if "token" not in flags:
            flags.append("token")
        redacted = _NOTION_TOKEN_RE.sub(_REDACTION, redacted)
    framework_findings = detect_credentials(redacted)
    if framework_findings:
        if "credential" not in flags:
            flags.append("credential")
        redacted = _redact_spans(redacted, framework_findings)
    return redacted


class ValidateInputNode(FunctionNode):
    """Validate + S-2 flag-and-redact the inbound Notion content brief."""

    # S-1: first line of the read path, no privileged operation - default permissive.
    required_trust_level = TrustLevel.ANONYMOUS

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input") or ""

        # The outer graph serialized the request into a JSON string; accept both
        # the serialized shape and a bare string for direct unit testing.
        text = raw
        parent_hint = state.get("parent_hint", "")
        if isinstance(raw, str) and raw.strip().startswith("{"):
            try:
                obj = json.loads(raw)
                text = obj.get("text", "")
                parent_hint = obj.get("parent_hint", parent_hint)
            except (ValueError, TypeError):
                text = raw

        if not isinstance(text, str) or len(text.strip()) < _MIN_LEN:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["ValidateInputNode: empty or non-brief input"],
            }

        # S-2 deterministic flag-and-redact (before any logging).
        flags: list[str] = []
        redacted = _scan_and_redact(text, flags)
        # The context channel carries caller text too and the framework's PII mask
        # covers user_input only, so the parent hint goes through the SAME scan.
        # A screen applied to one caller channel and not the other is not a screen.
        parent_hint = _scan_and_redact(parent_hint, flags) if parent_hint else parent_hint

        # S-4: audit the S-2 scan outcome - redaction flags only, never the inbound text.
        emit_trace_event(
            "input_validated",
            {"has_parent_hint": bool(parent_hint), "redaction_flags": flags},
            state,
        )

        return {
            "validated_input": redacted.strip(),
            "parent_hint": parent_hint,
            # ADR-005 / ARCH-2318: composite values cross the State boundary as
            # JSON strings, never native lists.
            "redaction_flags": to_json(flags),
            "status": AgentStatus.SUCCESS.value,
        }
