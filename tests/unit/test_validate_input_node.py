# CMN-C2-237 - Unit tests: ValidateInputNode (inner Step 1)
# Mirrors the sibling ToolCallingAgent's tests/unit/test_validate_input_node.py (Jira -> Notion).

import json

from framework.schemas.agent_status import AgentStatus
from src.nodes.validate_input_node import ValidateInputNode


class TestValidateInputNode:
    def setup_method(self):
        self.node = ValidateInputNode()

    def test_success_plain_text(self):
        state = {"validated_input": "Create a launch plan page", "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "Create a launch plan page"
        # redaction_flags crosses State as a JSON string (ADR-005/ARCH-2318)
        assert json.loads(result["redaction_flags"]) == []

    def test_success_serialized_json_input(self):
        payload = json.dumps({"text": "meeting notes for the launch", "parent_hint": "Workspace Home"})
        state = {"validated_input": payload, "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["validated_input"] == "meeting notes for the launch"
        assert result["parent_hint"] == "Workspace Home"

    def test_empty_input_errors(self):
        state = {"validated_input": "  ", "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    def test_short_input_errors(self):
        state = {"validated_input": "ab", "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_s2_redacts_email_and_token(self):
        text = "brief by alice@example.com integration secret_abcdef123456 for the launch page"
        state = {"validated_input": text, "node_history": [], "error_log": []}
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "alice@example.com" not in result["validated_input"]
        assert "secret_abcdef123456" not in result["validated_input"]
        flags = json.loads(result["redaction_flags"])
        assert "email" in flags
        assert "token" in flags

    def test_s2_flags_every_framework_credential_class(self):
        """Detector parity: this node's flag set must be a SUPERSET of the
        framework gate's block set, never narrower.

        A local set narrower than the framework's is a bypass: a value this node
        misses passes here, the framework raises inside a later node, and the
        wrapper then replaces that node's whole delta — clearing included — with
        a bare error partial.
        """
        from framework.security.credential_detector import detect_credentials

        for secret in (
            "sk_live_" + "51H8xQ2eZvKYlo2C0abcdefgh",
            "sk-abcdefghijklmnopqrstuvwxyz0123",
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
            "AKIAIOSFODNN7EXAMPLE",
            "Bearer abcdefghijklmnopqrstuvwxyz012345",
            "postgresql://reporting.example.invalid:5432/analytics",
        ):
            text = f"page brief for the launch using {secret} as the key"
            assert detect_credentials(text), f"probe is not framework-detectable: {secret}"
            result = self.node.execute({"validated_input": text, "node_history": [], "error_log": []})
            assert result["status"] == AgentStatus.SUCCESS.value
            assert secret not in result["validated_input"], secret
            assert "credential" in json.loads(result["redaction_flags"])

    def test_s2_redaction_leaves_ordinary_text_byte_identical(self):
        """The control: redaction must not rewrite an ordinary brief."""
        text = "Create a page for the Q3 launch. Owner: Sam. Budget: 100000. Ratio: 0.15."
        result = self.node.execute({"validated_input": text, "node_history": [], "error_log": []})
        assert result["validated_input"] == text
        assert json.loads(result["redaction_flags"]) == []

    def test_s2_overlapping_findings_do_not_splice(self):
        """`Bearer eyJ...` matches two framework patterns; merged spans must
        replace the region once rather than splicing a partial value back in."""
        secret = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        result = self.node.execute(
            {"validated_input": f"brief using {secret} for the page", "node_history": [], "error_log": []}
        )
        cleaned = result["validated_input"]
        assert "eyJ" not in cleaned
        assert cleaned.count("[REDACTED]") == 1
