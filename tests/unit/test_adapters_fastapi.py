"""Tests for the FastAPI / Starlette ASGI middleware (Phase 8, scenario 8).

Skipped automatically when starlette / fastapi are not installed (optional deps).
Uses Starlette's TestClient which runs the ASGI app in a thread pool and drives
the event loop internally, so pytest-asyncio is NOT needed here.

Scenario 8: X-Noukai-Session response header is set in capture mode and
matches the X-Session-Id that went out on the upstream request.
"""

import os
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

import httpx
import pytest

pytest.importorskip("starlette")
pytest.importorskip("fastapi")

from noukai_sdk import AsyncNoukai  # noqa: E402 — after importorskip guards
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


def _make_app(handler: Any) -> tuple[Any, AsyncNoukai]:
    """Build a minimal FastAPI app wired to the NoukaiTraceMiddleware.

    The app has a single POST / route that calls execute() via the client,
    returning the result in JSON. The mock transport handler is injected so
    tests can control what the SDK's HTTP calls return.
    """
    from fastapi import FastAPI

    from noukai_sdk.adapters.fastapi import NoukaiTraceMiddleware

    client = AsyncNoukai(api_key="nk_test", env="dev")
    client._transport._httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=client._transport._base_url,
    )

    app = FastAPI()
    app.add_middleware(NoukaiTraceMiddleware, client=client)

    @app.post("/")
    async def root() -> dict[str, Any]:
        result = await client.flow("acme/spelling/grade-3").execute(message="hi")
        return {"out": result.output}

    return app, client


# ---------------------------------------------------------------------------
# Scenario 8: capture mode — X-Noukai-Session header injected on response
# ---------------------------------------------------------------------------


def test_8_response_header_set_in_capture_mode() -> None:
    """Scenario 8 (FastAPI): capture mode sets X-Noukai-Session on the response
    and the same session_id goes out as X-Session-Id on the upstream call."""
    from starlette.testclient import TestClient

    captured_req_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_req_headers.update(dict(request.headers))
        return httpx.Response(200, json=_ok_execute_json())

    app, _ = _make_app(handler)
    with TestClient(app) as tc:
        resp = tc.post("/")

    assert resp.status_code == 200
    assert resp.json() == {"out": {"ok": True}}

    # Response header must be present (case-insensitive check).
    resp_header_keys = {k.lower() for k in resp.headers}
    assert HEADER_RESPONSE_SESSION.lower() in resp_header_keys

    # The session_id on the response matches the one sent upstream.
    resp_session_id = resp.headers.get(HEADER_RESPONSE_SESSION)
    upstream_session_id = captured_req_headers.get(HEADER_SESSION_ID.lower())
    assert resp_session_id is not None
    assert upstream_session_id is not None
    assert resp_session_id == upstream_session_id


# ---------------------------------------------------------------------------
# Replay mode — X-Noukai-Replay activates replay when env var is set
# ---------------------------------------------------------------------------


def test_8_replay_header_activates_replay_mode() -> None:
    """X-Noukai-Replay header + env var → replay mode; SDK serves recorded output."""
    from starlette.testclient import TestClient

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

    app, _ = _make_app(handler)
    with replay_enabled(), TestClient(app) as tc:
        resp = tc.post("/", headers={"X-Noukai-Replay": "11111111-1111-4111-8111-111111111111"})

    assert resp.status_code == 200
    assert resp.json() == {"out": {"replayed": True}}


def test_8_replay_header_ignored_when_env_unset() -> None:
    """Without NOUKAI_REPLAY_ENABLED, replay header is silently ignored → live call."""
    from starlette.testclient import TestClient

    captured_path: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_path.append(request.url.path)
        return httpx.Response(200, json=_ok_execute_json(result={"live": True}))

    app, _ = _make_app(handler)
    env_without_replay = {k: v for k, v in os.environ.items() if k != "NOUKAI_REPLAY_ENABLED"}
    with patch.dict(os.environ, env_without_replay, clear=True), TestClient(app) as tc:
        resp = tc.post("/", headers={"X-Noukai-Replay": "11111111-1111-4111-8111-111111111111"})

    assert resp.status_code == 200
    assert resp.json() == {"out": {"live": True}}
    # Must have gone to /execute (live path), not /sessions/
    assert any("/execute" in p for p in captured_path)


# ---------------------------------------------------------------------------
# Error mapping — replay backend errors → HTTP 4xx/5xx
# ---------------------------------------------------------------------------


def test_403_from_session_fetch_returns_403_response() -> None:
    """ReplayForbiddenError (backend 403) → middleware returns 403."""
    from starlette.testclient import TestClient

    def handler(request: httpx.Request) -> httpx.Response:
        if "/seq/sessions/" in request.url.path:
            return httpx.Response(
                403, json={"detail": {"code": "FORBIDDEN", "message": "no access"}}
            )
        pytest.fail("unexpected request")

    app, _ = _make_app(handler)
    with replay_enabled(), TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post("/", headers={"X-Noukai-Replay": "11111111-1111-4111-8111-111111111111"})

    assert resp.status_code == 403
    assert resp.json()["error"] == "replay_forbidden"


def test_404_from_session_fetch_returns_404_response() -> None:
    """ReplaySessionNotFoundError (backend 404) → middleware returns 404."""
    from starlette.testclient import TestClient

    def handler(request: httpx.Request) -> httpx.Response:
        if "/seq/sessions/" in request.url.path:
            return httpx.Response(
                404, json={"detail": {"code": "NOT_FOUND", "message": "no such session"}}
            )
        pytest.fail("unexpected request")

    app, _ = _make_app(handler)
    with replay_enabled(), TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post("/", headers={"X-Noukai-Replay": "11111111-1111-4111-8111-111111111111"})

    assert resp.status_code == 404
    assert resp.json()["error"] == "replay_session_not_found"


def test_non_http_scope_passes_through() -> None:
    """Non-HTTP scopes (e.g. lifespan) are passed through without modification."""
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from noukai_sdk.adapters.fastapi import NoukaiTraceMiddleware

    # lifespan events are exercised automatically by TestClient.__enter__
    client = AsyncNoukai(api_key="nk_test", env="dev")
    client._transport._httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})),
        base_url=client._transport._base_url,
    )
    app = FastAPI()
    app.add_middleware(NoukaiTraceMiddleware, client=client)

    @app.get("/ping")
    async def ping() -> dict[str, str]:
        return {"pong": "ok"}

    with TestClient(app) as tc:
        resp = tc.get("/ping")
    assert resp.status_code == 200
    assert resp.json() == {"pong": "ok"}
