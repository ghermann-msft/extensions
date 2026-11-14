from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPENHANDS_API_SKILL = ROOT / "skills" / "openhands-api" / "SKILL.md"
AGENT_CANVAS_SKILL = ROOT / "skills" / "agent-canvas-environment" / "SKILL.md"
PYTHON_HELPER = ROOT / "skills" / "openhands-api" / "scripts" / "openhands_api.py"
TYPESCRIPT_HELPER = ROOT / "skills" / "openhands-api" / "scripts" / "openhands_api.ts"


def test_openhands_api_skill_uses_hosted_delegation_configuration_first():
    text = OPENHANDS_API_SKILL.read_text(encoding="utf-8")
    hosted = text.index("### Hosted sandbox precedence")
    local = text.index("### Local Agent Canvas backend")

    assert hosted < local
    assert "both `OPENHANDS_CLOUD_API_URL` and `OPENHANDS_API_KEY` are present" in text
    assert "do not probe SaaS, Front Door, localhost services" in text
    assert "`/api/v1/users/me` is not a universal auth-validation endpoint" in text
    assert "include the current app conversation UUID as `parent_conversation_id`" in text
    assert "Never silently fall back to a top-level conversation" in text
    assert "The response is one start-task JSON object" in text
    assert "the API has no create idempotency key" in text
    assert "Never change the operation id merely to retry" in text
    assert "If `operation_id` is omitted, each helper invocation generates a new unique id" in text
    assert "A stale claim left before transmission can be reclaimed" in text


def test_agent_canvas_skill_falls_back_to_local_only_without_hosted_configuration():
    text = AGENT_CANVAS_SKILL.read_text(encoding="utf-8")
    selection = text.index("## Select the delegation path")
    local_key = text.index("## Find the session key")

    assert selection < local_key
    assert "POST /api/v1/app-conversations" in text
    assert "Only use the local Agent Canvas flow below when those two authoritative variables are not both present." in text
    assert "do not probe localhost, persisted Canvas keys, `/api/settings`, or `/api/v1/users/me` first" in text


def test_delegation_helpers_use_exact_payload_and_guard_duplicate_recovery():
    python = PYTHON_HELPER.read_text(encoding="utf-8")
    typescript = TYPESCRIPT_HELPER.read_text(encoding="utf-8")

    assert '"generate_title_on_submit": True' in python
    assert '"run": bool(run)' not in python
    assert "import httpx" not in python
    assert "_claim_conversation_operation" in python
    assert "OpenHandsAPIRequestRejectedError" in python
    assert "generate_title_on_submit: true" in typescript
    assert "run: req.run" not in typescript
    assert "appConversationStartGuarded" in typescript
    assert "already has an ${previous.status} create attempt" in typescript
