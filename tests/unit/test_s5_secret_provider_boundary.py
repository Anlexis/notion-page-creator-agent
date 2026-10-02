# S-5 secret-provider boundary regression.
#
# Fail-closed proof that the agent integration secret (NOTION_TOKEN) resolves
# ONLY through the configured namespaced SecretProvider (secrets_factory →
# DotenvProvider tiers) — NEVER the process environment. INVOKE_AUTH_TOKEN is
# the sole documented entry-point exception (covered by
# tests/proof_of_boundary/test_server_boot.py). STG mock mode may mint an
# ephemeral stub token, but only INTO the namespaced dotenv tier the provider
# reads — the resolution path stays provider-only in every mode.

import importlib
from pathlib import Path

import pytest


def _reload_server():
    """(Re)import the server so boot-time provisioning re-runs under the
    current cwd / environment (same pattern as test_server_boot.py)."""
    import src.api.server as srv

    return importlib.reload(srv)


@pytest.fixture(autouse=True)
def _restore_server_module():
    """Leave the module rebuilt from the real repo cwd for later test files."""
    yield
    _reload_server()


def test_env_token_never_resolves_through_provider(monkeypatch, tmp_path):
    """A NOTION_TOKEN in the process environment must NOT be visible through
    the agent's provider — the environment is not a credential source for
    agent secrets (fail-closed: with no dotenv tier the secret is unresolved)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STG_MOCK_MODE", raising=False)
    monkeypatch.setenv("NOTION_TOKEN", "env-canary-must-not-leak")
    srv = _reload_server()
    assert srv.agent._secrets_provider.get("NOTION_TOKEN") is None


def test_no_source_at_all_stays_unresolved(monkeypatch, tmp_path):
    """No env var, no dotenv tier, no mock mode → unresolved (None); the node
    layer then surfaces a clean status=error via ctx.secrets.require (covered
    by test_call_notion_api_node.py)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("STG_MOCK_MODE", raising=False)
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    srv = _reload_server()
    assert srv.agent._secrets_provider.get("NOTION_TOKEN") is None


def test_mock_mode_mints_into_namespaced_dotenv_tier(monkeypatch, tmp_path):
    """STG_MOCK_MODE mints a per-boot stub token INTO
    env/namespaces/{ns}/.env.{env} — resolved back through the provider — and
    an env-var NOTION_TOKEN still never wins."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STG_MOCK_MODE", "true")
    monkeypatch.setenv("AGENTCORE_ENVIRONMENT", "staging")
    monkeypatch.setenv("NOTION_TOKEN", "env-canary-must-not-leak")
    srv = _reload_server()
    token = srv.agent._secrets_provider.get("NOTION_TOKEN")
    assert token is not None and token.startswith("stub-")
    assert token != "env-canary-must-not-leak"
    dotenv = tmp_path / "env" / "namespaces" / "cmn-c2-237" / ".env.stg"
    assert dotenv.exists()
    assert token in dotenv.read_text(encoding="utf-8")


def test_mock_mode_never_overwrites_provisioned_token(monkeypatch, tmp_path):
    """An operator-provisioned NOTION_TOKEN in the dotenv tier wins; mock-mode
    minting must not touch it."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("STG_MOCK_MODE", "true")
    monkeypatch.setenv("AGENTCORE_ENVIRONMENT", "staging")
    dotenv = tmp_path / "env" / "namespaces" / "cmn-c2-237" / ".env.stg"
    dotenv.parent.mkdir(parents=True)
    dotenv.write_text("NOTION_TOKEN=operator-provisioned\n", encoding="utf-8")
    srv = _reload_server()
    assert srv.agent._secrets_provider.get("NOTION_TOKEN") == "operator-provisioned"
    assert dotenv.read_text(encoding="utf-8") == "NOTION_TOKEN=operator-provisioned\n"


def test_server_source_has_no_raw_env_read_of_agent_secret():
    """Static tripwire: src/api/server.py must never read the agent secret from
    the process environment (os.environ / getenv). The only sanctioned env
    reads are STG_MOCK_MODE (a mode flag) and INVOKE_AUTH_TOKEN (the
    entry-point exception)."""
    src = (Path(__file__).parents[2] / "src" / "api" / "server.py").read_text(encoding="utf-8")
    for pattern in (
        'os.environ.get("NOTION_TOKEN"',
        "os.environ.get('NOTION_TOKEN'",
        'os.environ["NOTION_TOKEN"]',
        'os.getenv("NOTION_TOKEN"',
    ):
        assert pattern not in src
