"""AgentCore Platform v1.0 - inner workflow Step 4: CallNotionApi (write side-effect).

Performs the real create/append write against the Notion REST API v1.

Security posture:
  S-1: required_trust_level = ANONYMOUS. The single external trust gate lives on
       the OUTER backbone pre_process (VERIFIED_EXTERNAL), not on this inner node.
       GraphNode.execute() passes the caller's InvocationContext into the inner
       subgraph UNCHANGED (no trust elevation), so a real external caller runs this
       write under its own VERIFIED_EXTERNAL context; declaring INTERNAL here would
       S-1-deny that already-gated external caller before the write ever runs (a
       deploy-stg first-invoke failure). The write therefore stays ANONYMOUS.
  S-3: the integration token is read via ctx.secrets.require("NOTION_TOKEN")
       (InvocationContext.from_state(state)) - never os.environ, never stored in state.
  S-4: emit_trace_event() is called on every write - a side-effect on an external
       workspace; HTTP 4xx/5xx surfaces as status=error + error_log (no silent pass).

Both entry points inject NotionClient.live() (stdlib http.client transport); the
network-free stub is used only by tests and the STG mock deploy. This node's
contract is identical whichever transport is injected.
"""

import concurrent.futures
import math
from collections.abc import Callable
from typing import Any

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.services.notion_client import NotionApiError, NotionClient

_SECRET_KEY = "NOTION_TOKEN"

# Bounds for the declared write deadline (config/config.yaml: timeout_s).
_TIMEOUT_DEFAULT = 30.0
_TIMEOUT_MIN = 0.001
_TIMEOUT_MAX = 300.0


def _finite_timeout(value: object) -> float:
    """Parse the declared timeout, failing CLOSED to the default.

    float("nan") and float("inf") both parse without error and then compare
    False against every bound, so a bare `float(value)` silently disables the
    deadline. Reject non-finite and out-of-range values explicitly.
    """
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _TIMEOUT_DEFAULT
    if not math.isfinite(parsed) or not (_TIMEOUT_MIN <= parsed <= _TIMEOUT_MAX):
        return _TIMEOUT_DEFAULT
    return parsed


class CallNotionApiNode(FunctionNode):
    """Create a Notion page / database record, or append blocks, via REST API v1."""

    # S-1: the external trust gate is enforced UPSTREAM on the outer backbone
    # pre_process (VERIFIED_EXTERNAL). This inner node runs under the caller's
    # UNELEVATED context (GraphNode does not elevate trust for the subgraph), so it
    # must stay ANONYMOUS - declaring INTERNAL would deny a real external caller
    # before the write runs.
    required_trust_level = TrustLevel.ANONYMOUS

    def __init__(
        self,
        notion_client: NotionClient | None = None,
        timeout_s: object = None,
    ) -> None:
        # notion_client is an immutable dependency (transport + base_url), not
        # per-invocation state - safe to hold on the instance. timeout_s is the
        # declared write deadline from config/config.yaml, forwarded by the outer
        # graph; it is the reader that makes that declaration load-bearing.
        self._client = notion_client
        self._timeout_s = _finite_timeout(timeout_s)

    def _with_deadline(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run a blocking transport call under the declared deadline.

        A hung transport must not hang the agent: the call runs on a worker
        thread and the node fails CLOSED when the deadline passes.
        """
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            return executor.submit(fn, *args).result(timeout=self._timeout_s)
        finally:
            # Do not block on a call that already overran its deadline.
            executor.shutdown(wait=False)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        payload = from_json(state.get("notion_payload"), None)
        if not payload:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallNotionApiNode: missing notion_payload"],
            }

        intent = state.get("intent", "create_page")

        # S-3: token from the bound secret provider - never os.environ / state.
        ctx = InvocationContext.from_state(state)
        try:
            api_token = ctx.secrets.require(_SECRET_KEY)
        except Exception:
            # Name the KEY, never the provider's exception text - a secret
            # provider's message can quote the value it failed to resolve.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallNotionApiNode: secret {_SECRET_KEY} unavailable"],
            }

        if self._client is None:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallNotionApiNode: no NotionClient configured (inject via graph config)"],
            }

        # S-4: audit the write side-effect. No credentials / PII in the payload -
        # only the intent and whether a parent was resolved.
        emit_trace_event(
            "notion_write",
            {"intent": intent, "has_parent_id": bool(state.get("parent_id", ""))},
            state,
        )

        try:
            if intent == "append_blocks":
                block_id = payload.get("block_id", "") or state.get("parent_id", "")
                if not block_id:
                    return {
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["CallNotionApiNode: append_blocks requires a resolved block/page id"],
                    }
                self._with_deadline(
                    self._client.append_blocks,
                    block_id,
                    payload.get("children", []),
                    api_token,
                )
                # append returns a block list (no page id/url) - echo the target.
                return {
                    "page_id": block_id,
                    "page_url": f"https://www.notion.so/{block_id.replace('-', '')}",
                    "status": AgentStatus.SUCCESS.value,
                }

            # create_page and create_database_record both POST /v1/pages.
            if not (payload.get("parent") or {}):
                return {
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["CallNotionApiNode: unresolved parent - cannot create page/record"],
                }
            resp = self._with_deadline(self._client.create_page, payload, api_token) or {}
        except concurrent.futures.TimeoutError:
            # Closed-set label: the deadline is ours, the elapsed time is not
            # caller data, and no transport detail crosses the boundary.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["CallNotionApiNode: Notion write exceeded the configured deadline"],
            }
        except NotionApiError as exc:
            # exc renders as "Notion API error <status>: <message>"; the message
            # comes from the Notion service, not from an internal traceback.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallNotionApiNode: {exc}"],
            }
        except Exception as exc:  # network / transport failure - no silent pass
            # Closed-set label plus the exception CLASS only: str(exc) on an
            # arbitrary transport exception can carry paths and connection strings,
            # but the class (URLError vs SSLError vs TimeoutError) is what tells an
            # egress block apart from a TLS failure.
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": [f"CallNotionApiNode: Notion write failed (transport error: {type(exc).__name__})"],
            }

        return {
            "page_id": resp.get("id", ""),
            "page_url": resp.get("url", ""),
            "status": AgentStatus.SUCCESS.value,
        }
