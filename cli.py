"""AGENTIC STAR Marketplace entrypoint — one-shot Pod process.

Referenced by this repo's Dockerfile as the image `CMD`. Compiles the agent,
provisions its secrets, then hands off to shared.bootstrap.marketplace_app
for the Marketplace lifecycle (identity, input, events, terminal delivery,
exit). Mirrors agentcore's own `agents/base/chat_agent/cli.py` (the pattern
this file was copied from).

`namespace=` here is the Marketplace secret-provisioning namespace — a different
concept from `config/agent.yaml`'s AgentRegistry `namespace:` key that happens to
share its value. Mirrors
`src/api/server.py`'s existing `secrets_factory(namespace="cmn-c2-237",
agent_name="NotionPageCreatorAgent")` call shape rather than a per-template value: one
Marketplace Pod deploys exactly one template, so there is no cross-template
secret-path collision to guard against.
"""

import logging
from pathlib import Path

from framework.utils.config_loader import load_agent_config
from shared.bootstrap.marketplace_app import run_agent_marketplace
from src.graph.graph import NotionPageCreatorAgent
from src.services.notion_client import NotionClient

# Add config overrides here to set values without touching config/config.yaml.
# The Notion client is a constructed object, so it rides here, not in the YAML.
# It holds no credential: NOTION_TOKEN is read per call via ctx.secrets.
extend_config = {"notion_client": NotionClient.live()}

if __name__ == "__main__":
    # S-4 audit records (incl. node_error with the failing node's error_log) go to
    # the "agentcore.audit" logger at INFO; with no handler the Pod log drops them
    # and a failed run surfaces only status='error'.
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("agentcore.audit").setLevel(logging.INFO)
    run_agent_marketplace(
        NotionPageCreatorAgent,
        agent_name="NotionPageCreatorAgent",
        namespace="cmn-c2-237",
        config={**load_agent_config(Path(__file__).resolve().parent), **extend_config},
    )
