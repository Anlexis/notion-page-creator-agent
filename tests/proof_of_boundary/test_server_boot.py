# PB: server-boot + S-5 caller-auth boundary (CMN-C2-237)
#
# Proves the standalone HTTP entry point (src/api/server.py) IMPORTS and
# CONSTRUCTS with no raise, and that the /invoke caller-auth credential
# (INVOKE_AUTH_TOKEN) is read through the framework SecretProvider accessor
# (S-5) — never a raw os.environ read in the request handler — while the
# Bearer→VERIFIED_EXTERNAL elevation still authenticates the deploy-path caller.

import importlib

import pytest


def _reload_server():
    """(Re)import the server so the boot-time entry-point provider re-reads env."""
    import src.api.server as srv

    return importlib.reload(srv)


def test_server_imports_and_constructs_without_token(monkeypatch):
    """Boot-safe: importing/constructing the server with NO token must not raise,
    and the entry-point provider surfaces None (→ ANONYMOUS callers)."""
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    srv = _reload_server()
    assert srv.app is not None
    assert srv.agent is not None
    # S-5: the token is accessed via the SecretProvider accessor, not os.environ.
    assert srv._ENTRYPOINT_SECRETS.get("INVOKE_AUTH_TOKEN") is None


def test_entrypoint_provider_surfaces_deploy_env_token(monkeypatch):
    """The deploy path (deploy-stg / local-stg.yml) delivers INVOKE_AUTH_TOKEN via
    the process env; the boot-time SecretProvider loads it so the handler reads it
    through .get() rather than os.environ."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "deploy-token-abc")
    srv = _reload_server()
    from framework.secrets import SecretProvider

    assert isinstance(srv._ENTRYPOINT_SECRETS, SecretProvider)
    assert srv._ENTRYPOINT_SECRETS.get("INVOKE_AUTH_TOKEN") == "deploy-token-abc"


def _test_client(srv):
    starlette_testclient = pytest.importorskip("starlette.testclient")
    return starlette_testclient.TestClient(srv.app)


def test_invoke_auth_boundary_via_secret_provider(monkeypatch):
    """The Bearer→VERIFIED_EXTERNAL elevation still authenticates using the
    provider-read token: no/incorrect Bearer → 401; correct Bearer → auth passes
    (crosses the S-1 boundary; not 401)."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "deploy-token-abc")
    srv = _reload_server()
    client = _test_client(srv)

    body = {"input": "create a page titled Hi", "session_id": "s"}
    assert client.post("/invoke", json=body).status_code == 401
    assert client.post("/invoke", json=body, headers={"authorization": "Bearer nope"}).status_code == 401
    ok = client.post("/invoke", json=body, headers={"authorization": "Bearer deploy-token-abc"})
    assert ok.status_code != 401  # auth boundary crossed


def test_no_token_configured_allows_anonymous(monkeypatch):
    """When no token is configured, /invoke does not 401 (ANONYMOUS callers per
    STG_RUNBOOK §5)."""
    monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
    srv = _reload_server()
    client = _test_client(srv)
    r = client.post("/invoke", json={"input": "hi", "session_id": "s"})
    assert r.status_code != 401
