# CMN-C2-237 - Unit tests: config/agent.yaml manifest + config/config.yaml runtime.
#
# The manifest is FLAT: AgentRegistry reads every key at ROOT level, so there is
# no `agent:` block. Runtime parameters live in config/config.yaml and are read
# by src.graph.graph.runtime_config().

import pathlib

import pytest

try:
    import yaml  # pyyaml (transitive dep of the framework wheel)

    _YAML_ERROR = None
except Exception as exc:  # pragma: no cover
    yaml = None
    _YAML_ERROR = exc

_CONFIG_DIR = pathlib.Path(__file__).parents[2] / "config"
_MANIFEST_PATH = _CONFIG_DIR / "agent.yaml"
_RUNTIME_PATH = _CONFIG_DIR / "config.yaml"

pytestmark = pytest.mark.skipif(_YAML_ERROR is not None, reason=f"pyyaml unavailable: {_YAML_ERROR}")


def _load(path=_MANIFEST_PATH):
    return yaml.safe_load(path.read_text())


def test_manifest_is_flat():
    """Regression pin: a nested `agent:` block is the retired shape. Any reader
    pointed at it returns {} on every call and its configuration goes dead."""
    data = _load()
    assert "agent" not in data
    assert "config" not in data


def test_manifest_identity():
    data = _load()
    assert data["id"] == "CMN-C2-237"
    assert data["category"] == "Cat 2"
    assert data["industry"] == "CMN"
    assert data["base_type"] == "ToolCallingAgent"
    assert data["namespace"] == "cmn"


def test_manifest_entry_point():
    data = _load()
    # Single dotted import path (module.Class), not split module:/class:.
    assert data["class"] == "src.graph.graph.NotionPageCreatorAgent"


def test_manifest_security():
    data = _load()
    # Agent-level entry trust, enforced by the outer backbone pre_process gate
    # (VERIFIED_EXTERNAL); the inner write node stays ANONYMOUS (S-1).
    assert data["required_trust_level"] == "VERIFIED_EXTERNAL"
    # The Notion integration token is a declared invoke-time secret. It is
    # reached via ctx.secrets.require(), so declaring it is correct: a declared
    # secret that is never require()d would 503 the agent at compile time.
    assert "NOTION_TOKEN" in data["requires"]["secrets"]
    assert data["requires"]["extras"] == []


def test_runtime_config_keys_all_have_readers():
    """Every key in config/config.yaml must be one this template consumes.

    A declared runtime value with no reader is dead configuration — the failure
    mode is silent: the code degrades to its in-code default and the declaration
    reads as if it were in force.
    """
    from src.graph.graph import _RUNTIME_DEFAULTS

    declared = set(_load(_RUNTIME_PATH) or {})
    assert declared <= set(
        _RUNTIME_DEFAULTS
    ), f"config/config.yaml declares keys with no reader: {declared - set(_RUNTIME_DEFAULTS)}"


def test_runtime_config_reaches_the_graph_config():
    """The declared values must land on the graph config the framework reads."""
    from src.graph.graph import NotionPageCreatorAgent, runtime_config

    declared = runtime_config()
    agent = NotionPageCreatorAgent()
    assert agent.config["max_retry"] == declared["max_retry"]
    assert agent.config["timeout_s"] == declared["timeout_s"]
    # An explicitly-passed key still wins (AgentRegistry / test injection).
    assert NotionPageCreatorAgent(config={"max_retry": 7}).config["max_retry"] == 7
