"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly.

import os
import re
import secrets
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.security.credential_detector import detect_credentials_in_value
from framework.secrets import SecretProvider
from framework.secrets.context import bound_secrets
from shared.secrets import ENV_NORMALIZE, InMemoryProvider
from shared.secrets import factory as secrets_factory
from shared.utils.settings import PlatformSettings
from src.graph.graph import NotionPageCreatorAgent
from src.services.notion_client import NotionClient

app = FastAPI(title="Agent")

_NAMESPACE = "cmn-c2-237"
_AGENT_NAME = "NotionPageCreatorAgent"

# STG provisional deploy runs in mock mode (scaffold deploy-stg sets
# STG_MOCK_MODE="true"): no live Notion workspace, no real NOTION_TOKEN in the
# job env — the first-invoke smoke test must still complete the backbone
# end-to-end (status=success) using the default network-free stub transport.
_STG_MOCK_MODE = os.environ.get("STG_MOCK_MODE", "").lower() == "true"


def _ensure_stg_mock_agent_secret() -> None:
    """STG mock mode ONLY: seed the ephemeral NOTION_TOKEN into the dotenv tier.

    S-5 (secret-provider boundary, ESC-CMN-C2-237-S5): agent secrets resolve
    ONLY through the configured namespaced SecretProvider (secrets_factory →
    DotenvProvider). The process environment is NEVER a source for agent
    secrets — INVOKE_AUTH_TOKEN is the narrowly documented entry-point
    exception (see _entrypoint_secrets); NOTION_TOKEN is not. Real deploys
    provision NOTION_TOKEN in env/namespaces/{namespace}/.env.{env} (see
    docs/07_operation_guide.md) and this helper does nothing.

    In the scaffold deploy-stg provisional run (STG_MOCK_MODE="true") there is
    no live Notion workspace and no provisioned token, but the first-invoke
    smoke test must still complete end-to-end. So BEFORE the provider is
    constructed, mint a per-boot ephemeral token with secrets.token_urlsafe()
    INTO the namespaced dotenv tier — the same file the configured provider
    reads — keeping the resolution path provider-only even in mock mode. The
    stub transport (src/services/notion_client.py) never authenticates, so the
    value is inert. NOTHING is hardcoded (credential-scan / S-4 clean); an
    already-provisioned NOTION_TOKEN in the file is never overwritten. Outside
    mock mode a missing token stays unresolved and CallNotionApiNode surfaces
    a clean status=error (fail-closed).
    """
    if not _STG_MOCK_MODE:
        return
    env = ENV_NORMALIZE.get(PlatformSettings().environment)
    if env is None:
        # Unknown env name — mint nothing; secrets_factory() below raises the
        # factory's own loud ValueError for the same condition.
        return
    path = Path.cwd() / "env" / "namespaces" / _NAMESPACE / f".env.{env}"
    if path.exists() and "NOTION_TOKEN=" in path.read_text(encoding="utf-8"):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("NOTION_TOKEN=stub-" + secrets.token_urlsafe(24) + "\n")


def _notion_client() -> NotionClient:
    """NotionClient injected into the agent config (forwarded to the inner graph).

    In mock mode (STG provisional deploy) the default NETWORK-FREE stub transport
    is used, so the smoke write completes end-to-end with no live Notion workspace
    — the deploy-time equivalent of the fake transport PB-6 injects. A real deploy
    gets the live stdlib transport.
    """
    if _STG_MOCK_MODE:
        return NotionClient()  # default network-free stub transport
    return NotionClient.live()


agent = NotionPageCreatorAgent(config={"notion_client": _notion_client()})
agent.compile()
_ensure_stg_mock_agent_secret()
agent.provision_secrets(secrets_factory(namespace=_NAMESPACE, agent_name=_AGENT_NAME))


def _entrypoint_secrets() -> SecretProvider:
    """Boot-time SecretProvider for the ENTRY-POINT / DEPLOYMENT credential.

    S-5 (secret-provider boundary): the caller-auth token INVOKE_AUTH_TOKEN is a
    *deployment-level* credential — it authenticates the /invoke caller at the
    standalone HTTP boundary, BEFORE any InvocationContext (and thus ctx.secrets)
    exists — so it is a different class of secret from the agent's per-invocation
    secrets (NOTION_TOKEN, resolved through ctx.secrets.require()).

    The framework SecretProvider is still the sanctioned access boundary: this
    loads the deployment credential from its documented source (the process
    environment set by the deploy job, whose STG rehearsal exports the SAME env
    value as a Bearer token) INTO an InMemoryProvider once
    at boot — exactly as shared.secrets.factory() loads dotenv values into a
    DotenvProvider — so the request handler reads the token via the provider
    accessor (.get()) rather than a raw os.environ read. Boot-safe: an
    absent/empty token yields a provider whose .get() returns None, preserving
    the "no token set → ANONYMOUS callers" contract with no raise at import.

    PATTERN NOTE (for other templates): entry-point/deployment credentials that
    arrive via the process environment are loaded into an InMemoryProvider here;
    agent secrets that arrive via env/*.env files stay on secrets_factory() and
    are read node-side through ctx.secrets.require().
    """
    values = {}
    token = os.environ.get("INVOKE_AUTH_TOKEN")
    if token:
        values["INVOKE_AUTH_TOKEN"] = token
    return InMemoryProvider(values, namespace=_NAMESPACE, agent_name=_AGENT_NAME)


_ENTRYPOINT_SECRETS = _entrypoint_secrets()


# Field names are caller data too: echo one back only when it is an ordinary
# identifier AND trips no credential pattern itself.
_SAFE_FIELD_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _screen_input_context(context: dict[str, Any]) -> str | None:
    """Return the LOCATION of a credential-shaped value in input_context, else None.

    Why this exists (measured against the installed wheel): the framework's
    InitializeNode returns `input_context` verbatim into its own result, and the
    @final S-3 gate scans every value of every result - so a credential-shaped
    string anywhere in input_context makes the FIRST node fail with an opaque
    `status: error` and a traceback in error_log, before any template code runs.
    The request cannot succeed either way, so convert the opaque failure into an
    actionable 400 that names the field.

    Reproduced on the plain standalone /invoke path with a single UNDECLARED key:
    validators ignore undeclared keys, and ignoring is not stripping - the key
    still rides into state. Declaring an inert context contract is not immunity.

    Uses the framework's own detector, so this refusal set matches the gate's
    block set exactly and cannot drift. `detect_credentials_in_value(dict)` is
    defined as the union over `.values()`, so per-field iteration is exactly
    equivalent to scanning the whole mapping - that identity is what lets this
    name the field without widening or narrowing the block set.
    """
    for index, (name, value) in enumerate(context.items(), start=1):
        if not detect_credentials_in_value(value):
            continue
        label = str(name)
        if not _SAFE_FIELD_NAME.match(label) or detect_credentials_in_value(label):
            label = f"field #{index}"
        return f"input_context.{label}"
    return None


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Caller-supplied, read-only write-target context (parent_id / parent_hint /
    # database_id). PreProcessNode reads it from state["input_context"] to resolve
    # the Notion parent; a create/append with no resolvable parent surfaces a clean
    # status=error. The write target is NEVER inferred from the free-text input —
    # it must be supplied here (risk mitigation), so the HTTP boundary carries it.
    input_context: dict[str, Any] = {}


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Any:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone/STG caller auth: when
    # INVOKE_AUTH_TOKEN is set on the server environment, callers that no upstream
    # middleware vouched for (still ANONYMOUS) must present it as a Bearer token
    # and run at VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary (standalone equivalent of
    # platform AuthMiddleware) — a deployment-level caller credential, not an
    # agent secret, so ctx.secrets does not apply (no InvocationContext exists
    # before auth); this is the documented entry-point exception to the
    # secret-provider boundary rule.
    #
    # S-5: the token is read through the framework SecretProvider accessor
    # (_ENTRYPOINT_SECRETS.get(), built once at boot from the deployment env),
    # never via a raw os.environ read in the handler.
    expected = _ENTRYPOINT_SECRETS.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    # Screen input_context BEFORE invoke(): a credential-shaped value there
    # detonates the framework's first node with an opaque error. 400, not 422 —
    # pydantic owns 422 and returns a list of error objects there, so reusing it
    # makes client handling ambiguous. Name the FIELD, never the value.
    offending = _screen_input_context(req.input_context)
    if offending:
        raise HTTPException(
            status_code=400,
            detail=f"Credential-shaped value rejected in {offending}.",
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        return agent.invoke(req.input, ctx=ctx, input_context=req.input_context)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "NotionPageCreatorAgent"}
