# openhands-api

Reference skill + minimal clients for the **OpenHands Cloud API** (V1) and common **agent-server APIs**.

This skill now also covers the **multi-conversation delegation pattern**: start additional Cloud conversations when you want fresh context windows, background work, or parallel tasks.

It also covers direct local Agent Canvas backend conversations when authoritative hosted delegation variables are absent and you need to call a local agent server with `X-Session-API-Key` instead of creating an app conversation.

- Skill instructions and endpoint overview: [`SKILL.md`](./SKILL.md)
- Minimal Python client: [`scripts/openhands_api.py`](./scripts/openhands_api.py)
- Minimal TypeScript client: [`scripts/openhands_api.ts`](./scripts/openhands_api.ts)
- References: [`references/README.md`](./references/README.md)

The Python helper uses only the standard library; no package installation is required.

## Quick start

```bash
export OPENHANDS_CLOUD_API_URL="https://your-openhands-app"
export OPENHANDS_CLOUD_API_KEY="..."
python skills/openhands-api/scripts/openhands_api.py search-conversations --limit 5
```

The Python client uses `OPENHANDS_CLOUD_API_URL` when set and otherwise defaults to `https://app.all-hands.dev`. In a hosted sandbox where `OPENHANDS_CLOUD_API_URL` and `OPENHANDS_API_KEY` are both present, that pair takes precedence for app-conversation delegation; do not probe local Agent Canvas or `/api/v1/users/me` first. Outside that hosted configuration, the client prefers `OPENHANDS_CLOUD_API_KEY` and falls back to `OPENHANDS_API_KEY`.

## Delegating work with new Cloud conversations

Use `POST /api/v1/app-conversations` to create a separate OpenHands Cloud conversation for a self-contained task, then poll `GET /api/v1/app-conversations/start-tasks?ids=...` until you have an `app_conversation_id`. Share the Agent Canvas link from `OpenHandsAPI.app_conversation_ui_url(app_conversation_id)` (the conversation's `conversation_ui_url`, falling back to `<base_url>/canvas/conversations/<id>`).

Keep delegated prompts self-contained: include the repository, branch, relevant files, constraints, and expected output. Prefer five or fewer concurrently running delegated conversations.

For an explicit subconversation request, pass the current app conversation UUID as `parent_conversation_id`. The Python helper can discover it from the hosted sandbox's internal agent-server catalog with `OpenHandsAPI.current_agent_server_conversation_id()` when that catalog contains exactly one conversation. Supply one stable `operation_id` to `app_subconversation_start()` and reuse it during recovery; the helper retries stale pre-send claims and definitive failures but will not issue a second create after delivery becomes uncertain. Omitting `operation_id` generates a unique id for that invocation, so identical intentional calls remain independent but cannot be recovered by a caller-supplied id after a crash.

`app_conversation_start_guarded()` provides the same operation ledger for top-level creates. The Python CLI applies `--operation-id` to both top-level and child creates. Python state updates are atomic and persistent; TypeScript protection is process-local.

## Start-task vs app conversation id

In many deployments, `POST /api/v1/app-conversations` returns a **start-task** object.

- `id` is the **start_task_id**
- `app_conversation_id` is what you should use for `/download` and `/conversation/.../events/...`

If `app_conversation_id` is missing from the initial response, fetch it via:

- `GET /api/v1/app-conversations/start-tasks?ids=<start_task_id>`

(If you accidentally use a start-task id with `/download`, you’ll get `404 Not Found`.)

The initial message shape is exactly `{"role":"user","content":[...]}`. Do not add the local agent-server `run` field to an app-conversation request.

## Source of truth

This skill is aligned against:

- `OpenHands/docs/openhands/usage/cloud/cloud-api.mdx`
- `OpenHands/docs/openhands/usage/agent-canvas/backend-setup/local.mdx`
- `OpenHands/docs/sdk/arch/agent-server.mdx`
- `OpenHands/docs/openhands/usage/api/v1.mdx`
- `OpenHands/OpenHands/openhands/app_server/v1_router.py`
- `OpenHands/OpenHands/openhands/app_server/app_conversation/app_conversation_router.py`
- `OpenHands/OpenHands/openhands/app_server/app_conversation/app_conversation_models.py`
