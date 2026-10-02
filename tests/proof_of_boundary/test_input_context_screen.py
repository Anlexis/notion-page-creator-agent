# PB: input_context credential screen at the HTTP adapter.
#
# Measured against the installed wheel: InitializeNode returns `input_context`
# verbatim into its own result, and the @final S-3 gate scans every value of every
# result — so a credential-shaped string ANYWHERE in input_context makes the FIRST
# node fail with an opaque `status: error` and a traceback in error_log, before any
# template code runs. Reproduced on the plain standalone /invoke path with a single
# UNDECLARED key: validators ignore undeclared keys, and ignoring is not stripping.
#
# The request cannot succeed either way, so the adapter converts the opaque failure
# into an actionable 400 that names the FIELD and never the value.

import importlib

import pytest

try:
    from framework.security.credential_detector import detect_credentials_in_value
    from shared.secrets.inmemory_provider import InMemoryProvider

    from src.graph.graph import NotionPageCreatorAgent
    from src.services.notion_client import NotionClient

    _IMPORT_ERROR = None
except Exception as exc:  # pragma: no cover
    _IMPORT_ERROR = exc

pytestmark = pytest.mark.skipif(_IMPORT_ERROR is not None, reason=f"framework wheel unavailable: {_IMPORT_ERROR}")

_PARENT_ID = "0a1b2c3d4e5f60718293a4b5c6d7e8f9"
_TOKEN = "screen-probe-token"
_STRIPE = "sk_live_" + "51H8xQ2eZvKYlo2C0abcdefgh"
_AWS = "AKIAIOSFODNN7EXAMPLE"
_DB_URI = "postgresql://reporting.example.invalid:5432/analytics"


def _fake_post(url, headers, body):
    return 200, {"object": "page", "id": "page-scr", "url": "https://www.notion.so/pagescr"}


def _client(monkeypatch):
    starlette_testclient = pytest.importorskip("starlette.testclient")
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    srv = importlib.reload(importlib.import_module("src.api.server"))
    agent = NotionPageCreatorAgent(config={"notion_client": NotionClient(post=_fake_post)})
    agent.compile()
    agent.provision_secrets(InMemoryProvider({"NOTION_TOKEN": "mock-token-for-testing"}))
    srv.agent = agent
    return starlette_testclient.TestClient(srv.app)


def _post(client, context):
    return client.post(
        "/invoke",
        json={
            "input": 'Create a page titled "Launch Plan" for the release',
            "session_id": "screen",
            "input_context": context,
        },
        headers={"authorization": f"Bearer {_TOKEN}"},
    )


def test_ordinary_domain_context_still_passes(monkeypatch):
    """The control: the screen must not refuse an ordinary write target."""
    response = _post(_client(monkeypatch), {"parent_id": _PARENT_ID})
    assert response.status_code == 200
    assert response.json()["status"] == "success"


@pytest.mark.parametrize(
    "context,expected_field",
    [
        ({"parent_id": _STRIPE}, "input_context.parent_id"),
        ({"parent_id": _PARENT_ID, "trace_token": _AWS}, "input_context.trace_token"),
        ({"parent_id": _PARENT_ID, "db": _DB_URI}, "input_context.db"),
        # Nested — detect_credentials_in_value recurses dicts and lists.
        ({"parent_id": _PARENT_ID, "meta": {"k": _AWS}}, "input_context.meta"),
        ({"parent_id": _PARENT_ID, "items": [_STRIPE]}, "input_context.items"),
    ],
)
def test_credential_shaped_context_is_refused_readably(monkeypatch, context, expected_field):
    response = _post(_client(monkeypatch), context)
    # 400, not 422: pydantic owns 422 and returns a list of error objects there,
    # so reusing it makes client handling ambiguous.
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert expected_field in detail
    # The FIELD is named; the VALUE never is.
    for secret in (_STRIPE, _AWS, _DB_URI):
        assert secret not in detail


def test_unsafe_field_name_is_masked_not_echoed(monkeypatch):
    """Field names are caller data too — an unsafe name becomes a positional label."""
    response = _post(_client(monkeypatch), {"a b<script>": _AWS})
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "field #1" in detail
    assert "<script>" not in detail


def test_refusal_set_equals_the_framework_block_set():
    """Anti-drift property: this screen's decision must be exactly the
    framework's. detect_credentials_in_value(dict) is defined as the union over
    .values(), so per-field iteration is exactly equivalent to scanning the whole
    mapping — that identity is what lets the screen name a field without
    widening or narrowing the block set."""
    from src.api.server import _screen_input_context

    for context in (
        {},
        {"parent_id": _PARENT_ID},
        {"parent_id": _STRIPE},
        {"parent_id": _PARENT_ID, "trace_token": _AWS},
        {"parent_id": _PARENT_ID, "meta": {"k": _DB_URI}},
        {"parent_id": _PARENT_ID, "note": "an ordinary sentence about the launch"},
    ):
        refused = _screen_input_context(context) is not None
        assert refused == bool(detect_credentials_in_value(context)), context
