# PB-2 + PB-5: State Safety Verification
# Verifies that State contains only msgpack-safe types (no Pydantic, dataclass, JWT)

import ast
import os
import re
import pytest


CREDENTIAL_FIELD_PATTERNS = re.compile(
    r"(jwt|token|api_key|secret|password|credential|connection_string)", re.IGNORECASE
)

PROHIBITED_TYPE_ANNOTATIONS = [
    "BaseModel",
    "InvocationContext",
    # ARCH-2318 flat/JSON-safe State contract: no unbounded Any, no native
    # container annotations — composite data crosses State as JSON strings.
    # ast.dump() name forms, precise to avoid substring false-positives.
    "Name(id='Any'",
    "Name(id='list'",
    "Name(id='dict'",
]


def _scan_state_file(filepath: str) -> list[str]:
    """Scan a state definition file for safety violations."""
    with open(filepath, "r") as f:
        source = f.read()
        tree = ast.parse(source, filename=filepath)

    violations = []

    for node in ast.walk(tree):
        # Check class definitions that look like State
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    field_name = item.target.id

                    # Check for credential-like field names
                    if CREDENTIAL_FIELD_PATTERNS.search(field_name):
                        violations.append(f"{filepath}:{item.lineno} — Credential-like field name: {field_name}")

                    # Check for prohibited type annotations
                    if item.annotation:
                        annotation_str = ast.dump(item.annotation)
                        for prohibited in PROHIBITED_TYPE_ANNOTATIONS:
                            if prohibited in annotation_str:
                                violations.append(f"{filepath}:{item.lineno} — Prohibited type in State: {prohibited}")

    return violations


class TestStateSafety:
    """PB-2/PB-5: State must be msgpack-safe with no credentials."""

    def test_state_file_safety(self):
        """State definition must not contain credential fields or prohibited types."""
        state_file = os.path.join(os.path.dirname(__file__), "..", "..", "src", "schemas", "state.py")
        if not os.path.exists(state_file):
            pytest.skip("src/schemas/state.py not found")

        violations = _scan_state_file(state_file)

        assert violations == [], "State safety violations found:\n" + "\n".join(violations)


def _assert_checkpoint_safe(merged: dict) -> None:
    """Every merged value must be a msgpack/JSON-safe primitive; composite data
    only as JSON strings. error_log is the framework's list[str] contract."""
    import json as _json

    for key, value in merged.items():
        if key == "error_log":
            assert isinstance(value, list), f"{key}: framework contract is list[str]"
            assert all(isinstance(v, str) for v in value), f"{key}: non-str entry"
            continue
        assert value is None or isinstance(value, (str, int, float, bool)), (
            f"{key}: native {type(value).__name__} in merged State (ARCH-2318 — "
            "composite values must cross as JSON strings)"
        )
    # The two JSON-string carriers must round-trip when present.
    for key in ("result", "redaction_flags"):
        if merged.get(key) is not None:
            _json.loads(merged[key])


class TestOuterMergeCheckpointSafety:
    """PB-2/PB-5 extension (ARCH-2318): the outer GraphNode-returned State is
    checkpoint-safe — for both the real inner get_output() shape and a
    defensively-handled native-container sub_result."""

    def _node(self):
        from src.graph.graph import NotionWorkflowGraphNode

        return NotionWorkflowGraphNode()

    def test_merge_output_is_checkpoint_safe_on_inner_shape(self):
        from src.schemas.state import to_json

        sub_result = {
            "output": to_json({"page_id": "p1", "page_url": "u", "confirmation": "c"}),
            "status": "success",
            "intent": "create_page",
            "parent_id": "par-1",
            "page_id": "p1",
            "page_url": "u",
            "page_title": "T",
            "confirmation": "c",
            "notion_payload": to_json({"parent": {"page_id": "par-1"}}),
            "redaction_flags": to_json(["email"]),
            "error_log": [],
        }
        merged = self._node().merge_output({}, sub_result)
        _assert_checkpoint_safe(merged)

    def test_merge_output_serializes_native_containers(self):
        """A hand-built sub_result with native list/dict values must still merge
        to JSON strings — the boundary guard, not the caller, owns safety."""
        sub_result = {
            "output": {"page_id": "p1"},
            "redaction_flags": ["email", "token"],
            "status": "success",
            "error_log": ["note"],
        }
        merged = self._node().merge_output({}, sub_result)
        _assert_checkpoint_safe(merged)
        assert merged["result"] == '{"page_id":"p1"}'
        assert merged["redaction_flags"] == '["email","token"]'
