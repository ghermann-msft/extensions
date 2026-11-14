from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPENHANDS_API_SKILL = ROOT / "skills" / "openhands-api" / "SKILL.md"
AGENT_CANVAS_SKILL = ROOT / "skills" / "agent-canvas-environment" / "SKILL.md"


def test_openhands_api_skill_uses_hosted_delegation_configuration_first():
    text = OPENHANDS_API_SKILL.read_text(encoding="utf-8")
    hosted = text.index("### Hosted sandbox precedence")
    local = text.index("### Local Agent Canvas backend")

    assert hosted < local
    assert "both `OPENHANDS_CLOUD_API_URL` and `OPENHANDS_API_KEY` are present" in text
    assert "do not probe SaaS, Front Door, localhost services" in text
    assert "`/api/v1/users/me` is not a universal auth-validation endpoint" in text


def test_agent_canvas_skill_falls_back_to_local_only_without_hosted_configuration():
    text = AGENT_CANVAS_SKILL.read_text(encoding="utf-8")
    selection = text.index("## Select the delegation path")
    local_key = text.index("## Find the session key")

    assert selection < local_key
    assert "POST /api/v1/app-conversations" in text
    assert "Only use the local Agent Canvas flow below when those two authoritative variables are not both present." in text
    assert "do not probe localhost, persisted Canvas keys, `/api/settings`, or `/api/v1/users/me` first" in text
