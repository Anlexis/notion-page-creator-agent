"""S-1 input sanitizer + injection screen for CMN-C2-237 (outer backbone pre_process).

Pure, stateless domain helpers (NOT framework gate methods). Strip HTML markup,
cap length, and screen the caller's brief for prompt-injection before the text is
JSON-serialized and handed to the inner Notion workflow graph.

WHY THE TEMPLATE SCREENS AT ALL, when the framework's S-2 gate already refuses
these: an assertion that "the framework refused it" holds only where that gate is
active. Where it is absent or configured off, the payload reaches the answer path
and the agent returns SUCCESS - fail-OPEN. The node that owns the caller contract
owns the refusal, and the tests prove it by calling `execute()` directly with no
framework wrapper in front.
"""

from __future__ import annotations

import re

_HTML_TAG_RE = re.compile(r"<[^>]+>")

DEFAULT_MAX_LENGTH = 4000

# CHAT-TEMPLATE CONTROL TOKENS, screened as a CLASS rather than as a list of
# known names. `<|...|>` covers `<|im_start|>`, `<|system|>`, `<|endoftext|>` and
# every future sibling; the two bracket forms are the Llama-family markers. A
# screen built from directive PHRASES alone misses all of these, which is exactly
# how the token form gets through.
_CONTROL_TOKEN_RE = re.compile(
    r"<\|[^|>]{0,64}\|>|\[/?INST\]|<</?SYS>>",
    re.IGNORECASE,
)

# Directive phrases. Deliberately ANCHORED to imperative constructions: an
# unanchored pattern is the fail-CLOSED direction, and that is the one that
# blocks real work. A Notion brief may legitimately say "page for the system
# design review" or "ignore the old numbers in section 3" - neither may trip.
_DIRECTIVE_RE = re.compile(
    r"(?:ignore|disregard|forget|override)\s+(?:all\s+|any\s+|the\s+)?"
    r"(?:previous|prior|above|earlier|preceding|system)\s+"
    r"(?:instruction|instructions|prompt|prompts|rule|rules|message|messages)"
    r"|(?:ignore|disregard)\s+all\s+rules"
    r"|you\s+are\s+now\s+(?:a|an|the)\b"
    r"|act\s+as\s+(?:if\s+you\s+are\s+)?(?:a|an|the)\s+(?:system|admin|administrator|developer)\b"
    r"|reveal\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instructions)"
    r"|print\s+(?:your|the)\s+(?:system\s+)?prompt",
    re.IGNORECASE,
)

# Closed-set labels. A refusal names the CLASS and the FIELD, never the matched
# text - echoing the match hands the caller a working oracle for the screen.
REASON_CONTROL_TOKEN = "chat_template_control_token"
REASON_DIRECTIVE = "instruction_override_directive"


def sanitize_query(query: str, max_length: int = DEFAULT_MAX_LENGTH) -> str:
    """Strip HTML tags (injection/markup guard) and cap length."""
    cleaned = _HTML_TAG_RE.sub("", query)
    return cleaned[:max_length]


def screen_injection(text: str) -> str | None:
    """Return a closed-set reason label if `text` carries an injection, else None.

    Screens the string BOTH raw and markup-stripped, because the strip changes
    what is detectable in BOTH directions:

      * raw only - the strip silently removes `<|im_start|>` (it matches the HTML
        tag pattern) and forwards the directive residue, converting a detectable
        token attack into undetectable plain text;
      * stripped only - a spliced directive (`ig<b>nore</b> all previous
        instructions`) is invisible until the strip re-assembles it.

    Screening one and not the other leaves a live bypass either way.
    """
    if not isinstance(text, str) or not text:
        return None
    stripped = _HTML_TAG_RE.sub("", text)
    for candidate in (text, stripped):
        if _CONTROL_TOKEN_RE.search(candidate):
            return REASON_CONTROL_TOKEN
        if _DIRECTIVE_RE.search(candidate):
            return REASON_DIRECTIVE
    return None
