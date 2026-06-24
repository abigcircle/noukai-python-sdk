"""Tests for the Flask before_request / after_request adapter (Phase 8, scenario 8).

Skipped automatically when Flask is not installed (optional dev dep).

Scenario 8 (Flask): X-Noukai-Session header is set on the response in capture
mode, and the same session_id went out as X-Session-Id on the upstream SDK call.

Flask's test client is synchronous; the sync ``Noukai`` client is used here.
"""

import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import pytest

pytest.importorskip("flask")

from noukai_sdk import Noukai  # noqa: E402
from noukai_sdk._constants import HEADER_RESPONSE_SESSION, HEADER_SESSION_ID  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def replay_enabled():  # type: ignore[return]
    """Patch NOUKAI_REPLAY_ENABLED=true for the duration of the block."""
    with patch.dict(os.environ, {"NOUKAI_REPLAY_ENABLED": "true"}):
        yield


def _ok_execute_json(result: Any = None) -> dict[str, Any]:
    return {
        "status": "completed",
        "result": result if result is not None else {"ok": True},
        "executionId": "exec-1",
        "flowId": "flow-1",
        "blockCount": 1,
    }


def _make_flask_app(handler: Any) -> tuple[Any, Noukai]:
    """Build a minimal Flask app wired to init_noukai_trace.

    The app has a single POST / route that calls execute() via the sync client.
    The mock transport handler is injected so tests control what SDK HTTP calls return.
    """
    from flask import Flask, jsonify

    from noukai_sdk.adapters.flask import init_noukai_trace

    sdk_client = Noukai(api_key="nk_test", env="dev")
    sdk_client._transport._httpx_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=sdk_client._transport._base_url,
    )

    app = Flask(__name__)
    app.config["TESTING"] = True
    init_noukai_trace(app, client=sdk_client)

    @app.post("/")
    def root() -> Any:
        result = sdk_client.flow("acme/spelling/grade-3").execute(message="hi")
        return jsonify({"out": result.output})

    return app, sdk_client


# ---------------------------------------------------------------------------
# Scenario 8: capture mode
# ---------------------------------------------------------------------------


def test_8_response_header_set_in_capture_mode() -> None:
    """Scenario 8 (Flask): capture mode sets X-Noukai-Session on the response
    and the same session_id went out as X-Session-Id on the upstream call."""
    captured_req_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_req_headers.update(dict(request.headers))
        return httpx.Response(200, json=_ok_execute_json())

    app, _ = _make_flask_app(handler)
    with app.test_client() as tc:
        resp = tc.post("/")

    assert resp.status_code == 200
    assert resp.get_json() == {"out": {"ok": True}}

    # Response header present (Flask Headers are case-insensitive).
    resp_session_id = resp.headers.get(HEADER_RESPONSE_SESSION)
    assert resp_session_id is not None

    upstream_session_id = captured_req_headers.get(HEADER_SESSION_ID.lower())
    assert upstream_session_id is not None
    assert resp_session_id == upstream_session_id


# ---------------------------------------------------------------------------
# Replay mode
# ---------------------------------------------------------------------------


def test_8_replay_header_activates_replay_mode() -> None:
    """X-Noukai-Replay header + env var → replay mode; SDK serves recorded output."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/seq/sessions/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "sessionId": "11111111-1111-4111-8111-111111111111",
                    "executions": [
                        {
                            "executionId": "exec-rec-1",
                            "flowId": "flow-1",
                            "slug": "grade-3",
                            "triggerType": "execute",
                            "status": "completed",
                            "startedAt": "2026-06-05T00:00:00Z",
                            "completedAt": "2026-06-05T00:00:01Z",
                            "traceCaptureMode": "full",
                            "snapshotsAvailable": True,
                            "steps": [
                                {
                                    "stepId": "s-1",
                                    "blockId": "b-1",
                                    "attempt": 1,
                                    "inputSnapshot": {},
                                    "outputSnapshot": {"replayed": True},
                                    "errorSnapshot": None,
                                    "truncated": False,
                                    "startedAt": "2026-06-05T00:00:00Z",
                                    "completedAt": "2026-06-05T00:00:01Z",
                                }
                            ],
                            "errorAtStep": None,
                        }
                    ],
                },
            )
        pytest.fail(f"Unexpected outbound request: {request.url.path}")

    app, _ = _make_flask_app(handler)
    with replay_enabled(), app.test_client() as tc:
        resp = tc.post("/", headers={"X-Noukai-Replay": "11111111-1111-4111-8111-111111111111"})

    assert resp.status_code == 200
    assert resp.get_json() == {"out": {"replayed": True}}


def test_8_replay_header_ignored_when_env_unset() -> None:
    """Without NOUKAI_REPLAY_ENABLED, replay header is silently ignored → live call."""
    captured_path: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_path.append(request.url.path)
        return httpx.Response(200, json=_ok_execute_json(result={"live": True}))

    app, _ = _make_flask_app(handler)
    env_without_replay = {k: v for k, v in os.environ.items() if k != "NOUKAI_REPLAY_ENABLED"}
    with patch.dict(os.environ, env_without_replay, clear=True), app.test_client() as tc:
        resp = tc.post("/", headers={"X-Noukai-Replay": "11111111-1111-4111-8111-111111111111"})

    assert resp.status_code == 200
    assert resp.get_json() == {"out": {"live": True}}
    assert any("/execute" in p for p in captured_path)


# ---------------------------------------------------------------------------
# Error mapping — replay backend errors → HTTP 4xx
# ---------------------------------------------------------------------------


def test_403_from_session_fetch_returns_403_response() -> None:
    """ReplayForbiddenError (backend 403) → before_request returns 403."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/seq/sessions/" in request.url.path:
            return httpx.Response(
                403, json={"detail": {"code": "FORBIDDEN", "message": "no access"}}
            )
        pytest.fail("unexpected request")

    app, _ = _make_flask_app(handler)
    with replay_enabled(), app.test_client() as tc:
        resp = tc.post("/", headers={"X-Noukai-Replay": "11111111-1111-4111-8111-111111111111"})

    assert resp.status_code == 403
    assert resp.get_json()["error"] == "replay_forbidden"


def test_404_from_session_fetch_returns_404_response() -> None:
    """ReplaySessionNotFoundError (backend 404) → before_request returns 404."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/seq/sessions/" in request.url.path:
            return httpx.Response(
                404, json={"detail": {"code": "NOT_FOUND", "message": "no such session"}}
            )
        pytest.fail("unexpected request")

    app, _ = _make_flask_app(handler)
    with replay_enabled(), app.test_client() as tc:
        resp = tc.post("/", headers={"X-Noukai-Replay": "11111111-1111-4111-8111-111111111111"})

    assert resp.status_code == 404
    assert resp.get_json()["error"] == "replay_session_not_found"


def test_no_header_no_scope_attribute() -> None:
    """Requests without X-Noukai-Replay still get a session_id (capture mode)."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["sid"] = request.headers.get(HEADER_SESSION_ID.lower(), "")
        return httpx.Response(200, json=_ok_execute_json())

    app, _ = _make_flask_app(handler)
    with app.test_client() as tc:
        resp = tc.post("/")

    assert resp.status_code == 200
    # In capture mode a session_id is always generated.
    assert resp.headers.get(HEADER_RESPONSE_SESSION) is not None
    assert captured["sid"]  # SDK sent the X-Session-Id upstream
