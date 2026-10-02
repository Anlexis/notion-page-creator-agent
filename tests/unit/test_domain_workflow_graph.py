# CMN-C2-237 - Unit tests: inner NotionWorkflowGraph (BaseGraph) contract.
# The compiled/invoked path of the inner pipeline is exercised end-to-end by
# tests/integration/test_graph.py and tests/proof_of_boundary/test_pb_invoke_order.py;
# this module unit-checks the inner graph's public identity + output contract
# without needing a compiled subgraph.

import pytest

try:
    from src.graph.domain_workflow_graph import NotionWorkflowGraph
    from src.schemas.state import State

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover - only when the framework wheel is absent
    NotionWorkflowGraph = None
    State = None
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(_IMPORT_ERROR is not None, reason=f"framework wheel unavailable: {_IMPORT_ERROR}")


def _graph():
    return NotionWorkflowGraph(config={})


def test_inner_graph_identity():
    g = _graph()
    assert g.name == "notion_page_workflow"
    assert g.state_schema is State


def test_get_output_surfaces_page_fields():
    g = _graph()
    out = g.get_output(
        {
            "result": {"page_id": "p1", "confirmation": "Created page 'T'"},
            "status": "success",
            "intent": "create_page",
            "page_id": "p1",
            "page_url": "https://www.notion.so/p1",
            "page_title": "T",
            "confirmation": "Created page 'T'",
            "notion_payload": "{}",
            "redaction_flags": [],
            "error_log": [],
            "trace_id": "tr",
            "correlation_id": "co",
            "node_history": ["ValidateInputNode", "ConfirmNode"],
        }
    )
    assert out["status"] == "success"
    assert out["intent"] == "create_page"
    assert out["page_url"] == "https://www.notion.so/p1"
    assert out["confirmation"] == "Created page 'T'"
    assert out["output"] == {"page_id": "p1", "confirmation": "Created page 'T'"}


def test_get_output_carries_error_log():
    g = _graph()
    out = g.get_output({"status": "error", "error_log": ["boom"], "confirmation": ""})
    assert out["status"] == "error"
    assert out["error_log"] == ["boom"]


def test_get_output_has_no_or_fallback_on_non_success():
    """The inner graph must not recreate the framework's `formatted_output or
    result` hazard one level down.

    This method used to return `state.get("result") or state.get("confirmation")`.
    A gated-away (falsy) `result` re-opened the `or` and handed the caller the
    pre-gate confirmation — which carries the page id and url, i.e. the evidence
    that a write happened — under whatever status was set.
    """
    g = _graph()
    out = g.get_output(
        {
            "status": "error",
            "result": "",  # gated away
            "confirmation": "Created page 'T' - id=p1",  # the pre-gate answer
            "page_id": "p1",
            "page_url": "https://www.notion.so/p1",
            "page_title": "T",
            "notion_payload": '{"parent":{"page_id":"p1"}}',
            "intent": "create_page",
            "parent_id": "p1",
            "redaction_flags": '["email"]',
            "error_log": ["boom"],
        }
    )
    assert out["output"] is None
    blob = repr(out)
    for released in ("Created page", "p1", "notion.so"):
        assert released not in blob, f"{released!r} survived the non-success shape"


def test_get_output_keeps_evidence_on_the_gated_success_path():
    """The control: the same fields DO surface when the run actually succeeded,
    so a blank-everything implementation cannot pass the test above."""
    g = _graph()
    out = g.get_output(
        {
            "status": "success",
            "result": '{"page_id":"p1"}',
            "confirmation": "Created page 'T' - id=p1",
            "page_id": "p1",
            "page_url": "https://www.notion.so/p1",
            "page_title": "T",
            "notion_payload": "{}",
            "intent": "create_page",
            "parent_id": "parent-1",
            "redaction_flags": None,
            "error_log": [],
        }
    )
    assert out["output"] == '{"page_id":"p1"}'
    assert out["page_url"] == "https://www.notion.so/p1"
    assert out["confirmation"] == "Created page 'T' - id=p1"


def test_no_get_output_in_the_repo_uses_an_or_fallback():
    """AST guard over EVERY get_output in src/, not only the one on the agent class.

    The framework's `formatted_output or result` is the well-known instance of
    this bug, but a template can recreate it one level down in its own subgraph —
    and that one is ours to delete. Parsed rather than grepped so a docstring
    quoting the retired expression does not read as the expression.
    """
    import ast
    import pathlib

    src = pathlib.Path(__file__).parents[2] / "src"
    offenders = []
    for path in sorted(src.rglob("*.py")):
        if "examples" in path.parts:
            continue  # published blueprints, not production code
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef) and node.name == "get_output"):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.BoolOp) and isinstance(inner.op, ast.Or):
                    offenders.append(f"{path.name}:{inner.lineno}")
    assert not offenders, f"get_output carries an `or` fallback at: {offenders}"
