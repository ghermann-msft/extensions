import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest


def _load_openhands_api_module():
    skill_path = Path(__file__).parent.parent / "skills" / "openhands-api" / "scripts" / "openhands_api.py"
    spec = importlib.util.spec_from_file_location("openhands_api", skill_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["openhands_api"] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload
        self.content = b""
        self.headers = {}
        self.text = ""

    def json(self):
        return self.payload


class FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.posts = []
        self.gets = []
        self.post_responses = []
        self.get_responses = []

    def post(self, url, json=None, timeout=None, **kwargs):
        self.posts.append({"url": url, "json": json, "timeout": timeout, **kwargs})
        response = self.post_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return FakeResponse(response)

    def get(self, url, params=None, timeout=None):
        self.gets.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self.get_responses.pop(0))

    def close(self):
        return None


def _api(
    monkeypatch,
    mod,
    *,
    post_responses=None,
    get_responses=None,
    api_key="k",
    **kwargs,
):
    client = FakeClient()
    client.post_responses = list(post_responses or [])
    client.get_responses = list(get_responses or [])

    def create_client(**client_kwargs):
        client.kwargs = client_kwargs
        return client

    monkeypatch.setattr(mod, "_HTTPClient", create_client)
    return mod.OpenHandsAPI(api_key=api_key, **kwargs), client


def test_helper_has_no_third_party_runtime_dependency():
    source = (
        Path(__file__).parent.parent
        / "skills"
        / "openhands-api"
        / "scripts"
        / "openhands_api.py"
    ).read_text(encoding="utf-8")

    assert "import httpx" not in source


def test_typescript_helper_uses_delivery_aware_operation_semantics():
    helper_url = (
        Path(__file__).parent.parent
        / "skills"
        / "openhands-api"
        / "scripts"
        / "openhands_api.ts"
    ).as_uri()
    script = f"""
      import {{ OpenHandsAPI, OpenHandsAPIRequestNotSentError }} from {helper_url!r};
      let calls = 0;
      globalThis.fetch = async () => {{
        calls += 1;
        return {{
          ok: true,
          json: async () => ({{ id: "task-1", status: "WORKING" }}),
          text: async () => "",
        }};
      }};
      const first = new OpenHandsAPI({{ apiKey: "k" }});
      const second = new OpenHandsAPI({{ apiKey: "k" }});
      const request = {{
        initialMessage: "books",
        parentConversationId: "parent-1",
        operationId: "request-1",
      }};
      await first.appSubconversationStart(request);
      await second.appSubconversationStart(request);
      if (calls !== 1) process.exit(1);

      globalThis.fetch = async () => {{
        calls += 1;
        throw new Error("response lost");
      }};
      const uncertain = {{ ...request, operationId: "request-2" }};
      await first.appSubconversationStart(uncertain).catch(() => undefined);
      await second.appSubconversationStart(uncertain).catch(() => undefined);
      if (calls !== 2) process.exit(2);

      globalThis.fetch = async () => {{
        calls += 1;
        return {{
          ok: false,
          status: 400,
          statusText: "Bad Request",
          json: async () => ({{}}),
          text: async () => "rejected",
        }};
      }};
      const rejected = {{ ...request, operationId: "request-3" }};
      await first.appSubconversationStart(rejected).catch(() => undefined);
      globalThis.fetch = async () => {{
        calls += 1;
        return {{
          ok: true,
          json: async () => ({{ id: "task-3", status: "WORKING" }}),
          text: async () => "",
        }};
      }};
      await second.appSubconversationStart(rejected);
      if (calls !== 4) process.exit(3);

      globalThis.fetch = async () => {{
        calls += 1;
        throw new OpenHandsAPIRequestNotSentError("not sent");
      }};
      const notSent = {{ ...request, operationId: "request-not-sent" }};
      await first.appSubconversationStart(notSent).catch(() => undefined);
      globalThis.fetch = async () => {{
        calls += 1;
        return {{
          ok: true,
          json: async () => ({{ id: "task-not-sent", status: "WORKING" }}),
          text: async () => "",
        }};
      }};
      await second.appSubconversationStart(notSent);
      if (calls !== 6) process.exit(4);

      await first.appSubconversationStart({{
        initialMessage: "same task",
        parentConversationId: "parent-1",
      }});
      await first.appSubconversationStart({{
        initialMessage: "same task",
        parentConversationId: "parent-1",
      }});
      if (calls !== 8) process.exit(5);

      await first.appConversationStartGuarded({{
        initialMessage: "top level",
        operationId: "top-level-1",
      }});
      await second.appConversationStartGuarded({{
        initialMessage: "top level",
        operationId: "top-level-1",
      }});
      if (calls !== 9) process.exit(6);

      let release;
      globalThis.fetch = () => new Promise((resolve) => {{
        calls += 1;
        release = () => resolve({{
          ok: true,
          json: async () => ({{ id: "task-4", status: "WORKING" }}),
          text: async () => "",
        }});
      }});
      const concurrent = {{
        initialMessage: "concurrent",
        parentConversationId: "parent-1",
        operationId: "request-4",
      }};
      const active = first.appSubconversationStart(concurrent);
      await Promise.resolve();
      await second.appSubconversationStart(concurrent).catch(() => undefined);
      release();
      await active;
      if (calls !== 10) process.exit(7);
    """

    subprocess.run(
        ["node", "--experimental-strip-types", "--input-type=module", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )


def test_app_conversation_start_builds_exact_v1_payload(monkeypatch):
    mod = _load_openhands_api_module()
    api, client = _api(
        monkeypatch,
        mod,
        post_responses=[{"id": "task-1", "status": "WORKING"}],
        base_url="https://example.com/",
    )

    response = api.app_conversation_start(
        initial_message="hi",
        selected_repository="o/r",
        selected_branch="main",
        title="Test title",
        parent_conversation_id="parent-1",
    )

    assert response == {"id": "task-1", "status": "WORKING"}
    assert client.posts == [
        {
            "url": "https://example.com/api/v1/app-conversations",
            "json": {
                "generate_title_on_submit": True,
                "initial_message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "hi"}],
                },
                "selected_repository": "o/r",
                "selected_branch": "main",
                "title": "Test title",
                "parent_conversation_id": "parent-1",
            },
            "timeout": 120,
        }
    ]


def test_app_conversation_start_rejects_malformed_start_task(monkeypatch):
    mod = _load_openhands_api_module()
    api, _client = _api(monkeypatch, mod, post_responses=[{"unexpected": True}])

    with pytest.raises(mod.OpenHandsAPIError, match="invalid app-conversation start task"):
        api.app_conversation_start(initial_message="hi")


def test_app_conversation_start_omits_parent_for_top_level_conversation(monkeypatch):
    mod = _load_openhands_api_module()
    api, client = _api(
        monkeypatch, mod, post_responses=[{"id": "task-1", "status": "WORKING"}]
    )

    api.app_conversation_start(initial_message="hi")

    assert "parent_conversation_id" not in client.posts[0]["json"]


def test_current_agent_server_conversation_id_returns_only_unambiguous_match(monkeypatch):
    mod = _load_openhands_api_module()
    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return FakeResponse({"items": [{"id": "parent-1"}]})

    monkeypatch.setenv("OH_INTERNAL_SERVER_URL", "http://agent-server:8000/")
    monkeypatch.setenv("OH_SESSION_API_KEYS_0", "session-key")
    monkeypatch.setattr(mod, "_request", fake_request)

    assert mod.OpenHandsAPI.current_agent_server_conversation_id() == "parent-1"
    assert captured == {
        "method": "GET",
        "url": "http://agent-server:8000/api/conversations/search",
        "headers": {
            "X-Session-API-Key": "session-key",
            "Content-Type": "application/json",
        },
        "params": {"limit": 2},
        "timeout": 30,
    }


def test_current_agent_server_conversation_id_refuses_ambiguous_catalog(monkeypatch):
    mod = _load_openhands_api_module()
    monkeypatch.setattr(
        mod,
        "_request",
        lambda *args, **kwargs: FakeResponse(
            {"items": [{"id": "one"}, {"id": "two"}]}
        ),
    )

    assert (
        mod.OpenHandsAPI.current_agent_server_conversation_id(
            session_api_key="session-key"
        )
        is None
    )


def test_app_subconversation_start_fails_instead_of_creating_top_level(monkeypatch):
    mod = _load_openhands_api_module()
    api, _client = _api(monkeypatch, mod)
    monkeypatch.setattr(
        mod.OpenHandsAPI,
        "current_agent_server_conversation_id",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(ValueError, match="pass parent_conversation_id explicitly"):
        api.app_subconversation_start(initial_message="Talk about books")


def test_app_subconversation_start_reuses_completed_operation(monkeypatch, tmp_path):
    mod = _load_openhands_api_module()
    monkeypatch.setenv("OPENHANDS_API_STATE_DIR", str(tmp_path))
    api, client = _api(
        monkeypatch,
        mod,
        post_responses=[{"id": "task-1", "status": "WORKING"}],
    )
    request = {
        "initial_message": "Talk about books",
        "parent_conversation_id": "parent-1",
        "operation_id": "user-request-1",
    }

    first = api.app_subconversation_start(**request)
    second = api.app_subconversation_start(**request)

    assert first == second == {"id": "task-1", "status": "WORKING"}
    assert len(client.posts) == 1


def test_app_subconversation_start_without_operation_id_allows_identical_calls(
    monkeypatch, tmp_path
):
    mod = _load_openhands_api_module()
    monkeypatch.setenv("OPENHANDS_API_STATE_DIR", str(tmp_path))
    api, client = _api(
        monkeypatch,
        mod,
        post_responses=[
            {"id": "task-1", "status": "WORKING"},
            {"id": "task-2", "status": "WORKING"},
        ],
    )
    request = {
        "initial_message": "Talk about books",
        "parent_conversation_id": "parent-1",
    }

    first = api.app_subconversation_start(**request)
    second = api.app_subconversation_start(**request)

    assert first["id"] == "task-1"
    assert second["id"] == "task-2"
    assert len(client.posts) == 2


def test_app_conversation_operation_claim_is_atomic_under_concurrency(
    monkeypatch, tmp_path
):
    mod = _load_openhands_api_module()
    monkeypatch.setenv("OPENHANDS_API_STATE_DIR", str(tmp_path))
    api, _client = _api(monkeypatch, mod)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocking_start(**kwargs):
        calls.append(kwargs)
        entered.set()
        release.wait(timeout=5)
        return {"id": "task-1", "status": "WORKING"}

    monkeypatch.setattr(api, "app_conversation_start", blocking_start)
    request = {
        "initial_message": "Talk about books",
        "parent_conversation_id": "parent-1",
        "operation_id": "user-request-1",
    }
    result = {}

    def run_first():
        result["task"] = api.app_subconversation_start(**request)

    thread = threading.Thread(target=run_first)
    thread.start()
    assert entered.wait(timeout=5)

    state_file = next(tmp_path.glob("*.json"))
    assert json.loads(state_file.read_text(encoding="utf-8"))["status"] == "uncertain"
    with pytest.raises(mod.OpenHandsAPIError, match="do not POST again"):
        api.app_subconversation_start(**request)

    release.set()
    thread.join(timeout=5)
    assert result["task"]["id"] == "task-1"
    assert len(calls) == 1


def test_stale_pre_send_claim_can_be_recovered(monkeypatch, tmp_path):
    mod = _load_openhands_api_module()
    monkeypatch.setenv("OPENHANDS_API_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OPENHANDS_API_CLAIM_TTL_SECONDS", "0")
    api, client = _api(
        monkeypatch,
        mod,
        post_responses=[{"id": "task-1", "status": "WORKING"}],
    )
    fingerprint = api._conversation_operation_fingerprint(
        initial_message="Talk about books",
        parent_conversation_id="parent-1",
        selected_repository=None,
        selected_branch=None,
        title=None,
    )
    api._claim_conversation_operation("user-request-1", fingerprint)

    task = api.app_subconversation_start(
        initial_message="Talk about books",
        parent_conversation_id="parent-1",
        operation_id="user-request-1",
    )

    assert task["id"] == "task-1"
    assert len(client.posts) == 1


def test_app_subconversation_start_never_reposts_after_uncertain_failure(
    monkeypatch, tmp_path
):
    mod = _load_openhands_api_module()
    monkeypatch.setenv("OPENHANDS_API_STATE_DIR", str(tmp_path))
    api, client = _api(
        monkeypatch,
        mod,
        post_responses=[TimeoutError("response lost"), {"id": "task-2", "status": "WORKING"}],
    )
    request = {
        "initial_message": "Talk about books",
        "parent_conversation_id": "parent-1",
        "operation_id": "user-request-1",
    }

    with pytest.raises(mod.OpenHandsAPIError, match="outcome is uncertain"):
        api.app_subconversation_start(**request)
    with pytest.raises(mod.OpenHandsAPIError, match="do not POST again"):
        api.app_subconversation_start(**request)

    assert len(client.posts) == 1


@pytest.mark.parametrize(
    "failure_name",
    ["OpenHandsAPIRequestRejectedError", "OpenHandsAPIRequestNotSentError"],
)
def test_definitive_create_failure_can_retry_same_operation(
    monkeypatch, tmp_path, failure_name
):
    mod = _load_openhands_api_module()
    monkeypatch.setenv("OPENHANDS_API_STATE_DIR", str(tmp_path))
    failure = getattr(mod, failure_name)("definitive failure")
    api, client = _api(
        monkeypatch,
        mod,
        post_responses=[failure, {"id": "task-2", "status": "WORKING"}],
    )
    request = {
        "initial_message": "Talk about books",
        "parent_conversation_id": "parent-1",
        "operation_id": "user-request-1",
    }

    with pytest.raises(type(failure), match="definitive failure"):
        api.app_subconversation_start(**request)
    task = api.app_subconversation_start(**request)

    assert task["id"] == "task-2"
    assert len(client.posts) == 2


def test_top_level_cli_create_honors_operation_id(monkeypatch, tmp_path, capsys):
    mod = _load_openhands_api_module()
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Top-level task", encoding="utf-8")
    api, _client = _api(monkeypatch, mod)
    captured = {}

    def guarded(**kwargs):
        captured.update(kwargs)
        return {"id": "task-1", "status": "WORKING"}

    monkeypatch.setattr(api, "app_conversation_start_guarded", guarded)
    monkeypatch.setattr(mod, "OpenHandsAPI", lambda **_kwargs: api)

    result = mod.main(
        [
            "start-conversation",
            "--prompt-file",
            str(prompt),
            "--operation-id",
            "top-level-request-1",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out)["id"] == "task-1"
    assert captured["operation_id"] == "top-level-request-1"


def test_client_prefers_configured_cloud_api_url(monkeypatch):
    mod = _load_openhands_api_module()
    monkeypatch.setenv("OPENHANDS_CLOUD_API_URL", "http://openhands-sandbox-server:3000/")
    monkeypatch.setenv("OPENHANDS_API_KEY", "sandbox-key")
    monkeypatch.setenv("OPENHANDS_CLOUD_API_KEY", "saas-key")
    api, client = _api(monkeypatch, mod, api_key=None)

    assert api.base_url == "http://openhands-sandbox-server:3000"
    assert client.kwargs["headers"]["Authorization"] == "Bearer sandbox-key"


def test_client_falls_back_to_saas_without_configured_url(monkeypatch):
    mod = _load_openhands_api_module()
    monkeypatch.delenv("OPENHANDS_CLOUD_API_URL", raising=False)
    monkeypatch.setenv("OPENHANDS_API_KEY", "legacy-key")
    monkeypatch.setenv("OPENHANDS_CLOUD_API_KEY", "cloud-key")
    api, client = _api(monkeypatch, mod, api_key=None)

    assert api.base_url == mod.DEFAULT_BASE_URL
    assert client.kwargs["headers"]["Authorization"] == "Bearer cloud-key"


def test_explicit_client_configuration_overrides_environment(monkeypatch):
    mod = _load_openhands_api_module()
    monkeypatch.setenv("OPENHANDS_CLOUD_API_URL", "http://openhands-sandbox-server:3000")
    monkeypatch.setenv("OPENHANDS_API_KEY", "sandbox-key")
    monkeypatch.setenv("OPENHANDS_CLOUD_API_KEY", "cloud-key")
    api, client = _api(
        monkeypatch, mod, api_key=None, base_url="https://app.example.com"
    )

    assert api.base_url == "https://app.example.com"
    assert client.kwargs["headers"]["Authorization"] == "Bearer cloud-key"


def test_app_conversations_get_batch_passes_ids(monkeypatch):
    mod = _load_openhands_api_module()
    api, client = _api(monkeypatch, mod, get_responses=[[{"id": "conv-1"}]])

    conversations = api.app_conversations_get_batch(ids=["conv-1", "conv-2"])

    assert conversations == [{"id": "conv-1"}]
    assert client.gets[0]["params"] == {"ids": ["conv-1", "conv-2"]}


def test_poll_start_task_until_ready_uses_start_task_endpoint(monkeypatch):
    mod = _load_openhands_api_module()
    api, _client = _api(monkeypatch, mod)
    states = [
        {"id": "task-1", "status": "WORKING"},
        {"id": "task-1", "status": "READY", "app_conversation_id": "conv-1"},
    ]
    monkeypatch.setattr(
        api, "app_conversation_start_task_get", lambda _task_id: states.pop(0)
    )
    monkeypatch.setattr(mod.time, "sleep", lambda *_args, **_kwargs: None)

    ready = api.poll_start_task_until_ready(
        "task-1", timeout_s=10, poll_interval_s=0.01, backoff_factor=1.0
    )

    assert ready["app_conversation_id"] == "conv-1"


def test_legacy_alias_still_exists(monkeypatch):
    mod = _load_openhands_api_module()
    api, _client = _api(monkeypatch, mod)

    assert isinstance(api, mod.OpenHandsV1API)


def _api_with_conversations(monkeypatch, conversations, base_url="https://example.com"):
    mod = _load_openhands_api_module()
    api, _client = _api(
        monkeypatch, mod, get_responses=[conversations], base_url=base_url
    )
    return api


def test_app_conversation_ui_url_uses_server_provided_link(monkeypatch):
    server_link = "https://app.example.com/canvas/conversations/conv-1"
    api = _api_with_conversations(
        monkeypatch, [{"id": "conv-1", "conversation_ui_url": server_link}]
    )

    assert api.app_conversation_ui_url("conv-1") == server_link


def test_app_conversation_ui_url_falls_back_to_agent_canvas_route(monkeypatch):
    api = _api_with_conversations(
        monkeypatch,
        [{"id": "conv-1", "conversation_url": "https://runtime.example.com/api/conversations/conv-1"}],
        base_url="https://example.com/",
    )

    assert api.app_conversation_ui_url("conv-1") == "https://example.com/canvas/conversations/conv-1"
