"""OpenHands Cloud API (V1) minimal client.

This file is intentionally:
- small (easy to copy into other repos)
- dependency-free (Python standard library only)
- opinionated in a helpful way (defaults to OpenHands Cloud)

Audience: AI agents.

The V1 API is hosted on the OpenHands app server under:
  {BASE_URL}/api/v1/...

Typical workflow for common operations:
1) List/search conversations: GET /api/v1/app-conversations/search
2) Start a conversation (creates sandbox): POST /api/v1/app-conversations
3) Monitor events for a conversation: GET /api/v1/conversation/{id}/events/search
4) (Optional) download trajectory: GET /api/v1/app-conversations/{id}/download

Note: Some operations happen against the *agent server* running inside a sandbox
(not the app server). Those endpoints use X-Session-API-Key instead of Bearer auth.

This client purposefully keeps responses as raw dicts/lists so agents can quickly
adapt it without strict schema maintenance.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import secrets
import socket
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://app.all-hands.dev"
BASE_URL_ENV_VAR = "OPENHANDS_CLOUD_API_URL"
PREFERRED_API_KEY_ENV_VARS = ("OPENHANDS_CLOUD_API_KEY", "OPENHANDS_API_KEY")
DEFAULT_INTERNAL_AGENT_SERVER_URL = "http://127.0.0.1:8000"
INTERNAL_AGENT_SERVER_URL_ENV_VAR = "OH_INTERNAL_SERVER_URL"
SESSION_API_KEY_ENV_VARS = (
    "OH_SESSION_API_KEYS_0",
    "SESSION_API_KEY",
    "OPENHANDS_API_KEY",
)


# Start-task statuses observed in the wild. These may evolve, so keep this centralized.
START_TASK_TERMINAL_STATUSES = frozenset(
    {"READY", "ERROR", "FAILED", "CANCELLED", "DONE", "COMPLETED"}
)


# Safety cap for paging calls. Keeps responses small and consistent across clients.
AGENT_EVENTS_SEARCH_MAX_LIMIT = 100
DEFAULT_OPERATION_STATE_DIR = Path.home() / ".cache" / "openhands-api" / "operations"
OPERATION_STATE_DIR_ENV_VAR = "OPENHANDS_API_STATE_DIR"
OPERATION_CLAIM_TTL_ENV_VAR = "OPENHANDS_API_CLAIM_TTL_SECONDS"
DEFAULT_OPERATION_CLAIM_TTL_SECONDS = 300.0


class OpenHandsAPIError(RuntimeError):
    pass


class OpenHandsAPIRequestRejectedError(OpenHandsAPIError):
    """The server returned a definitive HTTP rejection."""


class OpenHandsAPIRequestNotSentError(OpenHandsAPIError):
    """The request failed before it could be transmitted."""


@dataclass(frozen=True)
class _Response:
    content: bytes
    headers: dict[str, str]

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenHandsAPIError("OpenHands API returned invalid JSON") from exc


def _request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    json_body: Any = None,
    data: bytes | None = None,
    timeout: float = 30,
) -> _Response:
    if params:
        query = urlencode(params, doseq=True)
        url = f"{url}{'&' if '?' in url else '?'}{query}"
    request_headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    try:
        request = Request(url, data=data, headers=request_headers, method=method)
        with urlopen(request, timeout=timeout) as response:
            return _Response(
                content=response.read(),
                headers={key.lower(): value for key, value in response.headers.items()},
            )
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise OpenHandsAPIRequestRejectedError(
            f"OpenHands API error {exc.code} {exc.reason}: {body}"
        ) from exc
    except URLError as exc:
        reason = exc.reason
        known_pre_send_errnos = {
            errno.EADDRNOTAVAIL,
            errno.ECONNREFUSED,
            errno.EHOSTUNREACH,
            errno.ENETUNREACH,
        }
        if isinstance(reason, (ConnectionRefusedError, socket.gaierror)) or (
            isinstance(reason, OSError) and reason.errno in known_pre_send_errnos
        ):
            raise OpenHandsAPIRequestNotSentError(
                f"OpenHands API request was not sent: {reason}"
            ) from exc
        raise OpenHandsAPIError(f"OpenHands API request outcome is uncertain: {reason}") from exc
    except ValueError as exc:
        raise OpenHandsAPIRequestNotSentError(
            f"OpenHands API request was not sent: {exc}"
        ) from exc


class _HTTPClient:
    def __init__(self, *, headers: dict[str, str], timeout: float = 30):
        self.headers = headers
        self.timeout = timeout

    def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _Response:
        return _request(
            "GET",
            url,
            headers=self.headers,
            params=params,
            timeout=timeout or self.timeout,
        )

    def post(
        self,
        url: str,
        *,
        json: Any = None,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> _Response:
        return _request(
            "POST",
            url,
            headers={**self.headers, **(headers or {})},
            json_body=json,
            data=data,
            timeout=timeout or self.timeout,
        )

    def close(self) -> None:
        return None


@dataclass(frozen=True)
class OpenHandsAPIConfig:
    api_key: str
    base_url: str = DEFAULT_BASE_URL

    @property
    def api_v1_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/v1"


class OpenHandsAPI:
    """Minimal OpenHands Cloud API client for the supported V1 API."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        configured_base_url = os.getenv(BASE_URL_ENV_VAR)
        resolved_key = api_key
        if not resolved_key and base_url is None and configured_base_url:
            resolved_key = os.getenv("OPENHANDS_API_KEY")
        if not resolved_key:
            for env_name in PREFERRED_API_KEY_ENV_VARS:
                resolved_key = os.getenv(env_name)
                if resolved_key:
                    break
        if not resolved_key:
            env_list = ", ".join(PREFERRED_API_KEY_ENV_VARS)
            raise ValueError(f"Missing API key. Set one of: {env_list}, or pass api_key=...")

        resolved_base_url = base_url or configured_base_url or DEFAULT_BASE_URL
        self._cfg = OpenHandsAPIConfig(
            api_key=resolved_key,
            base_url=resolved_base_url.rstrip("/"),
        )
        self._client = _HTTPClient(
            headers={
                "Authorization": f"Bearer {self._cfg.api_key}",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    @property
    def base_url(self) -> str:
        return self._cfg.base_url

    @property
    def api_v1_url(self) -> str:
        return self._cfg.api_v1_url

    def close(self) -> None:
        self._client.close()

    # -----------------------------
    # App server endpoints (Bearer auth)
    # -----------------------------

    def users_me(self) -> dict[str, Any]:
        r = self._client.get(f"{self.api_v1_url}/users/me")
        return r.json()

    def app_conversations_search(self, *, limit: int = 20) -> dict[str, Any]:
        limit = max(1, int(limit))
        r = self._client.get(
            f"{self.api_v1_url}/app-conversations/search", params={"limit": limit}
        )
        return r.json()

    def app_conversations_count(self) -> dict[str, Any]:
        r = self._client.get(f"{self.api_v1_url}/app-conversations/count")
        return r.json()

    def app_conversations_get_batch(self, *, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        r = self._client.get(f"{self.api_v1_url}/app-conversations", params={"ids": ids})
        return r.json()

    def app_conversation_get(self, conversation_id: str) -> dict[str, Any] | None:
        items = self.app_conversations_get_batch(ids=[conversation_id])
        return items[0] if items else None

    def app_conversation_ui_url(self, conversation_id: str) -> str:
        """Browser link that opens the conversation in Agent Canvas.

        Share this link with users. `conversation_url` on the conversation record is
        the sandbox's agent-server API, not a page a user can open.
        """
        conversation = self.app_conversation_get(conversation_id) or {}
        # Servers that predate `conversation_ui_url` serve Agent Canvas at /canvas.
        return (
            conversation.get("conversation_ui_url")
            or f"{self.base_url}/canvas/conversations/{conversation_id}"
        )

    def sandboxes_search(self, *, limit: int = 20) -> dict[str, Any]:
        limit = max(1, int(limit))
        r = self._client.get(f"{self.api_v1_url}/sandboxes/search", params={"limit": limit})
        return r.json()

    def sandbox_specs_search(self, *, limit: int = 20) -> dict[str, Any]:
        limit = max(1, int(limit))
        r = self._client.get(
            f"{self.api_v1_url}/sandbox-specs/search", params={"limit": limit}
        )
        return r.json()

    def conversation_events_search(
        self, conversation_id: str, *, limit: int = 50
    ) -> dict[str, Any]:
        limit = max(1, int(limit))
        r = self._client.get(
            f"{self.api_v1_url}/conversation/{conversation_id}/events/search",
            params={"limit": limit},
        )
        return r.json()

    def conversation_events_count(self, conversation_id: str) -> dict[str, Any]:
        r = self._client.get(f"{self.api_v1_url}/conversation/{conversation_id}/events/count")
        return r.json()

    def app_conversation_start(
        self,
        *,
        initial_message: str,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
        title: str | None = None,
        parent_conversation_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a new V1 app conversation.

        WARNING: This typically creates a sandbox and may incur costs.

        In many deployments this endpoint is **asynchronous** and returns a **start-task** dict.
        Common fields:
        - `id`: the *start_task_id*
        - `app_conversation_id`: the id to use for `/download` and `/conversation/.../events/...`

        If `app_conversation_id` is missing from the initial response, fetch it via:
        - `GET /api/v1/app-conversations/start-tasks?ids=<start_task_id>`
          (see `app_conversation_start_task_get()` / `poll_start_task_until_ready()`).

        The payload structure here mirrors what the V1 app server expects:
        - initial_message.content is a list of content parts
        """

        payload: dict[str, Any] = {
            "initial_message": {
                "role": "user",
                # V1 expects `content` as a list of parts, even for a single text message.
                "content": [{"type": "text", "text": initial_message}],
            },
            "generate_title_on_submit": True,
        }
        if selected_repository:
            payload["selected_repository"] = selected_repository
        if selected_branch:
            payload["selected_branch"] = selected_branch
        if title:
            payload["title"] = title
        if parent_conversation_id:
            payload["parent_conversation_id"] = parent_conversation_id

        r = self._client.post(f"{self.api_v1_url}/app-conversations", json=payload, timeout=120)
        task = r.json()
        if not isinstance(task, dict) or not task.get("id") or not task.get("status"):
            raise OpenHandsAPIError(
                "OpenHands API returned an invalid app-conversation start task"
            )
        return task

    def app_conversations_start_tasks_get_batch(self, *, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        r = self._client.get(
            f"{self.api_v1_url}/app-conversations/start-tasks", params={"ids": ids}
        )
        tasks = r.json()
        if not isinstance(tasks, list):
            raise OpenHandsAPIError(
                "OpenHands API returned an invalid start-task batch"
            )
        return tasks

    def _conversation_operation_fingerprint(
        self,
        *,
        initial_message: str,
        parent_conversation_id: str | None,
        selected_repository: str | None,
        selected_branch: str | None,
        title: str | None,
    ) -> str:
        canonical = json.dumps(
            {
                "base_url": self.base_url,
                "initial_message": initial_message,
                "parent_conversation_id": parent_conversation_id,
                "selected_repository": selected_repository,
                "selected_branch": selected_branch,
                "title": title,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _operation_state_path(operation_id: str) -> Path:
        state_dir = Path(
            os.getenv(OPERATION_STATE_DIR_ENV_VAR, str(DEFAULT_OPERATION_STATE_DIR))
        )
        state_dir.mkdir(parents=True, exist_ok=True)
        safe_id = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        return state_dir / f"{safe_id}.json"

    @staticmethod
    def _write_operation_state(path: Path, state: dict[str, Any]) -> None:
        temporary = path.with_suffix(f".{secrets.token_hex(8)}.tmp")
        temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    @contextmanager
    def _operation_state_lock(path: Path):
        lock_path = path.with_suffix(".lock")
        deadline = time.monotonic() + 5.0
        while True:
            try:
                lock_path.mkdir()
                break
            except FileExistsError:
                try:
                    lock_age = time.time() - lock_path.stat().st_mtime
                    if lock_age > 30.0:
                        lock_path.rmdir()
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() >= deadline:
                    raise OpenHandsAPIError(
                        f"Timed out locking conversation operation state {path.name}"
                    )
                time.sleep(0.01)
        try:
            yield
        finally:
            try:
                lock_path.rmdir()
            except FileNotFoundError:
                pass

    @staticmethod
    def _read_operation_state(path: Path) -> dict[str, Any] | None:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except json.JSONDecodeError as exc:
            raise OpenHandsAPIError(
                f"Conversation operation state is corrupt: {path}"
            ) from exc
        if not isinstance(state, dict):
            raise OpenHandsAPIError(f"Conversation operation state is invalid: {path}")
        return state

    @staticmethod
    def _operation_claim_ttl_seconds() -> float:
        return float(
            os.getenv(
                OPERATION_CLAIM_TTL_ENV_VAR,
                str(DEFAULT_OPERATION_CLAIM_TTL_SECONDS),
            )
        )

    @classmethod
    def _claim_conversation_operation(
        cls, operation_id: str, fingerprint: str
    ) -> tuple[Path, str, dict[str, Any] | None]:
        path = cls._operation_state_path(operation_id)
        owner = secrets.token_hex(16)
        now = time.time()
        with cls._operation_state_lock(path):
            state = cls._read_operation_state(path)
            if state is None:
                cls._write_operation_state(
                    path,
                    {
                        "status": "claimed",
                        "operation_id": operation_id,
                        "fingerprint": fingerprint,
                        "owner": owner,
                        "claimed_at": now,
                    },
                )
                return path, owner, None
            if state.get("fingerprint") != fingerprint:
                raise OpenHandsAPIError(
                    f"Conversation operation {operation_id} was already used for a different request"
                )
            if state.get("status") == "created" and isinstance(
                state.get("start_task"), dict
            ):
                return path, owner, state["start_task"]
            if state.get("status") == "failed":
                cls._write_operation_state(
                    path,
                    {
                        "status": "claimed",
                        "operation_id": operation_id,
                        "fingerprint": fingerprint,
                        "owner": owner,
                        "claimed_at": now,
                    },
                )
                return path, owner, None
            if state.get("status") == "claimed":
                claimed_at = float(state.get("claimed_at") or 0)
                if now - claimed_at >= cls._operation_claim_ttl_seconds():
                    cls._write_operation_state(
                        path,
                        {
                            "status": "claimed",
                            "operation_id": operation_id,
                            "fingerprint": fingerprint,
                            "owner": owner,
                            "claimed_at": now,
                        },
                    )
                    return path, owner, None
            raise OpenHandsAPIError(
                f"Conversation operation {operation_id} already has a "
                f"{state.get('status', 'unknown')} create attempt; do not POST again"
            )

    @classmethod
    def _update_conversation_operation(
        cls,
        path: Path,
        owner: str,
        state: dict[str, Any],
    ) -> None:
        with cls._operation_state_lock(path):
            current = cls._read_operation_state(path)
            if current is None or current.get("owner") != owner:
                raise OpenHandsAPIError(
                    f"Conversation operation ownership was lost for {path.name}"
                )
            cls._write_operation_state(path, {**current, **state})

    def app_conversation_start_guarded(
        self,
        *,
        initial_message: str,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
        title: str | None = None,
        parent_conversation_id: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Start one app conversation with delivery-aware duplicate protection."""
        operation = operation_id or f"invocation-{uuid.uuid4()}"
        fingerprint = self._conversation_operation_fingerprint(
            initial_message=initial_message,
            parent_conversation_id=parent_conversation_id,
            selected_repository=selected_repository,
            selected_branch=selected_branch,
            title=title,
        )
        state_path, owner, existing_task = self._claim_conversation_operation(
            operation, fingerprint
        )
        if existing_task is not None:
            return existing_task
        self._update_conversation_operation(
            state_path,
            owner,
            {"status": "uncertain", "send_started_at": time.time()},
        )
        try:
            task = self.app_conversation_start(
                initial_message=initial_message,
                selected_repository=selected_repository,
                selected_branch=selected_branch,
                title=title,
                parent_conversation_id=parent_conversation_id,
            )
        except (OpenHandsAPIRequestRejectedError, OpenHandsAPIRequestNotSentError) as exc:
            self._update_conversation_operation(
                state_path,
                owner,
                {"status": "failed", "error": str(exc), "failed_at": time.time()},
            )
            raise
        except Exception as exc:
            self._update_conversation_operation(
                state_path,
                owner,
                {"status": "uncertain", "error": str(exc)},
            )
            raise OpenHandsAPIError(
                f"Conversation create outcome is uncertain for operation {operation}; "
                "do not retry with a new operation id"
            ) from exc
        self._update_conversation_operation(
            state_path,
            owner,
            {"status": "created", "start_task": task, "created_at": time.time()},
        )
        return task

    def app_subconversation_start(
        self,
        *,
        initial_message: str,
        parent_conversation_id: str | None = None,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
        title: str | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        """Start one child conversation without retrying an uncertain create."""
        parent_id = (
            parent_conversation_id
            or self.current_agent_server_conversation_id()
        )
        if not parent_id:
            raise ValueError(
                "Current conversation id is unavailable or ambiguous; "
                "pass parent_conversation_id explicitly"
            )
        return self.app_conversation_start_guarded(
            initial_message=initial_message,
            selected_repository=selected_repository,
            selected_branch=selected_branch,
            title=title,
            parent_conversation_id=parent_id,
            operation_id=operation_id,
        )

    def app_conversation_start_task_get(self, task_id: str) -> dict[str, Any] | None:
        items = self.app_conversations_start_tasks_get_batch(ids=[task_id])
        return items[0] if items else None

    def sandboxes_pause(self, sandbox_id: str) -> dict[str, Any]:
        r = self._client.post(f"{self.api_v1_url}/sandboxes/{sandbox_id}/pause", timeout=60)
        return r.json()

    def sandboxes_resume(self, sandbox_id: str) -> dict[str, Any]:
        r = self._client.post(f"{self.api_v1_url}/sandboxes/{sandbox_id}/resume", timeout=60)
        return r.json()

    def app_conversation_download_zip(
        self, app_conversation_id: str, *, output_file: str | Path
    ) -> dict[str, Any]:
        """Download a conversation trajectory zip to disk.

        Note: this endpoint expects the **app_conversation_id** (not the start-task id).
        """
        url = f"{self.api_v1_url}/app-conversations/{app_conversation_id}/download"
        r = self._client.get(url, timeout=60)
        out = Path(output_file)
        out.write_bytes(r.content)
        return {
            "file": str(out),
            "size": len(r.content),
            "content_type": r.headers.get("content-type"),
        }

    def count_events_via_trajectory_zip(
        self,
        app_conversation_id: str,
        *,
        zip_file: str | Path,
        extract_dir: str | Path,
    ) -> dict[str, Any]:
        """Fallback event counting: download trajectory zip, extract, count event files.

        This is heavier than calling a count endpoint, but it is still a single API call and
        also gives you the full exported event payloads.

        Cleanup (optional): this helper writes a zip file and extracts JSON events. If you
        want to clean up afterwards, you can remove them, e.g.:

        - `zip_path.unlink(missing_ok=True)`
        - `shutil.rmtree(extract_path, ignore_errors=True)`

        Returns a small summary dict including `event_count`.
        """

        zip_path = Path(zip_file)
        extract_path = Path(extract_dir)

        download_meta = self.app_conversation_download_zip(app_conversation_id, output_file=zip_path)
        extract_path.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_path)

        event_count = len(list(extract_path.glob("event_*.json")))
        has_meta = (extract_path / "meta.json").exists()

        return {
            "event_count": event_count,
            "has_meta": has_meta,
            "zip": download_meta,
            "extract_dir": str(extract_path),
        }

    # -----------------------------
    # Agent server endpoints (X-Session-API-Key)
    # -----------------------------

    @staticmethod
    def agent_headers(session_api_key: str) -> dict[str, str]:
        return {"X-Session-API-Key": session_api_key, "Content-Type": "application/json"}

    @staticmethod
    def current_agent_server_conversation_id(
        *,
        agent_server_url: str | None = None,
        session_api_key: str | None = None,
    ) -> str | None:
        """Return the current hosted conversation id when it is unambiguous.

        Hosted sandboxes expose the current conversation through their internal
        agent-server catalog, not a dedicated conversation-id environment variable.
        Refuse to guess if the catalog contains zero or multiple conversations.
        """
        resolved_url = (
            agent_server_url
            or os.getenv(INTERNAL_AGENT_SERVER_URL_ENV_VAR)
            or DEFAULT_INTERNAL_AGENT_SERVER_URL
        ).rstrip("/")
        resolved_key = session_api_key
        if not resolved_key:
            for env_name in SESSION_API_KEY_ENV_VARS:
                resolved_key = os.getenv(env_name)
                if resolved_key:
                    break
        if not resolved_key:
            return None

        r = _request(
            "GET",
            f"{resolved_url}/api/conversations/search",
            headers=OpenHandsAPI.agent_headers(resolved_key),
            params={"limit": 2},
            timeout=30,
        )
        data = r.json()
        items = data.get("items", []) if isinstance(data, dict) else []
        if len(items) != 1 or not isinstance(items[0], dict):
            return None
        conversation_id = items[0].get("id")
        return str(conversation_id) if conversation_id else None


    @staticmethod
    def _agent_event_filter_params(
        *,
        timestamp_gte: str | None = None,
        timestamp_lt: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if timestamp_gte is not None:
            params["timestamp__gte"] = timestamp_gte
        if timestamp_lt is not None:
            params["timestamp__lt"] = timestamp_lt
        if kind is not None:
            params["kind"] = kind
        if source is not None:
            params["source"] = source
        if body is not None:
            params["body"] = body
        return params

    def agent_events_search(
        self,
        *,
        agent_server_url: str,
        session_api_key: str,
        conversation_id: str,
        limit: int = 50,
        sort_order: str | None = None,
        timestamp_gte: str | None = None,
        timestamp_lt: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        body: str | None = None,
    ) -> dict[str, Any]:
        """Search events via the sandbox agent-server.

        Notes:
        - `limit` is capped at AGENT_EVENTS_SEARCH_MAX_LIMIT to avoid huge responses.
        - `sort_order` must be one of: "TIMESTAMP", "TIMESTAMP_DESC".
        - timestamp filters are passed as ISO-8601 strings (e.g. "2026-02-14T21:54:00Z").
          The server accepts both timezone-aware and naive datetimes.
        """

        url = f"{agent_server_url.rstrip('/')}/api/conversations/{conversation_id}/events/search"
        capped_limit = min(AGENT_EVENTS_SEARCH_MAX_LIMIT, max(1, int(limit)))
        params: dict[str, Any] = {"limit": capped_limit}
        if sort_order is not None:
            params["sort_order"] = sort_order
        params.update(
            self._agent_event_filter_params(
                timestamp_gte=timestamp_gte,
                timestamp_lt=timestamp_lt,
                kind=kind,
                source=source,
                body=body,
            )
        )

        r = _request(
            "GET",
            url,
            headers=self.agent_headers(session_api_key),
            params=params,
            timeout=30,
        )
        return r.json()

    def agent_events_count(
        self,
        *,
        agent_server_url: str,
        session_api_key: str,
        conversation_id: str,
        timestamp_gte: str | None = None,
        timestamp_lt: str | None = None,
        kind: str | None = None,
        source: str | None = None,
        body: str | None = None,
    ) -> int:
        """Count events via the sandbox agent-server.

        Timestamp filters are passed as ISO-8601 strings (e.g. "2026-02-14T21:54:00Z").
        """

        url = f"{agent_server_url.rstrip('/')}/api/conversations/{conversation_id}/events/count"
        params = self._agent_event_filter_params(
            timestamp_gte=timestamp_gte,
            timestamp_lt=timestamp_lt,
            kind=kind,
            source=source,
            body=body,
        )

        r = _request(
            "GET",
            url,
            headers=self.agent_headers(session_api_key),
            params=params,
            timeout=30,
        )
        return int(r.json())

    def agent_execute_bash(
        self,
        *,
        agent_server_url: str,
        session_api_key: str,
        command: str,
        cwd: str | None = None,
        timeout_s: int = 30,
    ) -> dict[str, Any]:
        url = f"{agent_server_url.rstrip('/')}/api/bash/execute_bash_command"
        payload: dict[str, Any] = {"command": command, "timeout": int(timeout_s)}
        if cwd:
            payload["cwd"] = cwd
        r = _request(
            "POST",
            url,
            headers=self.agent_headers(session_api_key),
            json_body=payload,
            timeout=60,
        )
        return r.json()

    def agent_download_file(
        self,
        *,
        agent_server_url: str,
        session_api_key: str,
        path: str,
        output_file: str | Path,
    ) -> dict[str, Any]:
        p = path if path.startswith("/") else f"/{path}"
        url = f"{agent_server_url.rstrip('/')}/api/file/download{p}"
        r = _request(
            "GET",
            url,
            headers=self.agent_headers(session_api_key),
            timeout=30,
        )
        out = Path(output_file)
        out.write_bytes(r.content)
        return {"file": str(out), "size": len(r.content)}

    def agent_upload_text_file(
        self,
        *,
        agent_server_url: str,
        session_api_key: str,
        path: str,
        content: str,
        content_type: str = "text/plain",
    ) -> dict[str, Any]:
        p = path if path.startswith("/") else f"/{path}"
        url = f"{agent_server_url.rstrip('/')}/api/file/upload{p}"
        filename = os.path.basename(p)
        boundary = f"----openhands-{secrets.token_hex(16)}"
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8") + content.encode("utf-8") + f"\r\n--{boundary}--\r\n".encode("utf-8")
        r = _request(
            "POST",
            url,
            headers={
                "X-Session-API-Key": session_api_key,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            data=body,
            timeout=30,
        )
        return r.json() if r.text else {"success": True}

    # -----------------------------
    # Convenience helpers
    # -----------------------------

    def app_conversation_start_from_prompt_files(
        self,
        prompt_file: str | Path,
        *,
        selected_repository: str | None = None,
        selected_branch: str | None = None,
        title: str | None = None,
        parent_conversation_id: str | None = None,
        operation_id: str | None = None,
        append_file: str | Path | None = None,
    ) -> dict[str, Any]:
        main_text = Path(prompt_file).read_text(encoding="utf-8")
        if append_file and Path(append_file).exists():
            tail = Path(append_file).read_text(encoding="utf-8")
            initial = f"{main_text}\n\n{tail}"
        else:
            initial = main_text

        if parent_conversation_id:
            return self.app_subconversation_start(
                initial_message=initial,
                selected_repository=selected_repository,
                selected_branch=selected_branch,
                title=title,
                parent_conversation_id=parent_conversation_id,
                operation_id=operation_id,
            )
        if operation_id:
            return self.app_conversation_start_guarded(
                initial_message=initial,
                selected_repository=selected_repository,
                selected_branch=selected_branch,
                title=title,
                operation_id=operation_id,
            )
        return self.app_conversation_start(
            initial_message=initial,
            selected_repository=selected_repository,
            selected_branch=selected_branch,
            title=title,
        )


    @staticmethod
    def _start_task_status(task: dict[str, Any] | None) -> str:
        return str((task or {}).get("status") or "").upper()

    def poll_start_task_until_ready(
        self,
        task_id: str,
        *,
        timeout_s: int = 10 * 60,
        poll_interval_s: float = 2.0,
        backoff_factor: float = 1.5,
        max_interval_s: float = 10.0,
        max_polls: int | None = None,
    ) -> dict[str, Any]:
        """Poll a start-task until it reaches a terminal state.

        This is the async companion to `POST /api/v1/app-conversations`.

        It is intentionally *polite*:
        - sleeps between requests
        - uses exponential backoff (capped by `max_interval_s`)
        - supports `max_polls` to cap the total number of API calls

        Terminal statuses are defined in START_TASK_TERMINAL_STATUSES.

        Raises:
            TimeoutError: if the task doesn't reach a terminal state in time.
        """

        deadline = time.monotonic() + float(timeout_s)
        interval = max(0.25, float(poll_interval_s))
        factor = max(1.0, float(backoff_factor))
        max_interval = max(interval, float(max_interval_s))

        polls = 0
        last: dict[str, Any] | None = None

        while True:
            if max_polls is not None and polls >= int(max_polls):
                raise TimeoutError(
                    f"Start task {task_id} did not reach terminal state (max_polls={max_polls}, last={last})"
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"Start task {task_id} did not reach terminal state in {timeout_s}s (last={last})"
                )

            last = self.app_conversation_start_task_get(task_id)
            polls += 1

            status = self._start_task_status(last)
            if status in START_TASK_TERMINAL_STATUSES:
                return last or {}

            sleep_s = min(interval, remaining)
            if sleep_s > 0:
                time.sleep(sleep_s)
            interval = min(max_interval, interval * factor)


OpenHandsV1API = OpenHandsAPI

def _cmd_search_conversations(args: argparse.Namespace) -> int:
    api = OpenHandsAPI(api_key=args.api_key, base_url=args.base_url)
    try:
        print(json.dumps(api.app_conversations_search(limit=args.limit), indent=2))
        return 0
    finally:
        api.close()


def _cmd_start_conversation(args: argparse.Namespace) -> int:
    api = OpenHandsAPI(api_key=args.api_key, base_url=args.base_url)
    try:
        resp = api.app_conversation_start_from_prompt_files(
            args.prompt_file,
            selected_repository=args.repo,
            selected_branch=args.branch,
            title=args.title,
            parent_conversation_id=args.parent_conversation_id,
            operation_id=args.operation_id,
            append_file=args.append_file,
        )
        print(json.dumps(resp, indent=2))
        return 0
    finally:
        api.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="openhands_api.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_search = sub.add_parser("search-conversations", help="GET /api/v1/app-conversations/search")
    p_search.add_argument(
        "--api-key",
        default=None,
        help="Defaults to OPENHANDS_CLOUD_API_KEY, then OPENHANDS_API_KEY",
    )
    p_search.add_argument(
        "--base-url",
        default=None,
        help=f"Defaults to {BASE_URL_ENV_VAR}, then {DEFAULT_BASE_URL}",
    )
    p_search.add_argument("--limit", type=int, default=5)
    p_search.set_defaults(func=_cmd_search_conversations)

    p_start = sub.add_parser("start-conversation", help="POST /api/v1/app-conversations from a prompt file")
    p_start.add_argument(
        "--api-key",
        default=None,
        help="Defaults to OPENHANDS_CLOUD_API_KEY, then OPENHANDS_API_KEY",
    )
    p_start.add_argument(
        "--base-url",
        default=None,
        help=f"Defaults to {BASE_URL_ENV_VAR}, then {DEFAULT_BASE_URL}",
    )
    p_start.add_argument("--prompt-file", required=True)
    p_start.add_argument("--append-file", default=None)
    p_start.add_argument("--repo", default=None)
    p_start.add_argument("--branch", default=None)
    p_start.add_argument("--title", default=None)
    p_start.add_argument(
        "--parent-conversation-id",
        default=None,
        help="Set parent_conversation_id to create a child conversation",
    )
    p_start.add_argument(
        "--operation-id",
        default=None,
        help="Stable create id reused during recovery; applies to child and top-level creates",
    )
    p_start.set_defaults(func=_cmd_start_conversation)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
