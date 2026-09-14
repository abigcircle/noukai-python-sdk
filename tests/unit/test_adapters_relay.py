"""Flow relay adapter tests (design 20260903-SDK-agent-relay, PR-1).

Covers the FastAPI ``mount_flow_relay`` and the Flask ``flow_relay_blueprint``:
bounds-before-parse, authorize-before-forward, nk_ bearer injection, verbatim
relay of upstream (status, body) including 4xx/5xx, and non-JSON normalization.

Auto-skipped when the optional framework deps are not installed.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytest.importorskip("starlette")
pytest.importorskip("fastapi")

from noukai_sdk import AsyncNoukai, Noukai  # noqa: E402
from noukai_sdk.adapters.relay import (  # noqa: E402
    RelayBounds,
    flow_relay_blueprint,
    mount_flow_relay,
)

PAUSED_BODY = {
    "status": "tool_calls_required",
    "executionId": "exec-1",
    "pausedAtStep": "s1",
    "iterationsUsed": 1,
    "toolCallMessages": [{"role": "assistant", "content": "call"}],
    "toolCalls": [{"id": "tc1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
    "accumulatedOutputs": {},
    "flowId": "flow-1",
    "blockCount": 2,
}
COMPLETED_BODY = {
    "status": "completed",
    "result": {"ok": True},
    "flowId": "flow-1",
    "blockCount": 2,
}


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------


def _make_fastapi_app(handler: Any, *, authorize: Any = None, **relay_kwargs: Any):
    from fastapi import FastAPI

    client = AsyncNoukai(api_key="nk_test", env="dev")
    client._transport._httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=client._transport._base_url
    )

    async def _allow(_request: Any) -> None:
        return None

    app = FastAPI()
    mount_flow_relay(
        app,
        client=client,
        org="acme",
        project="spelling",
        slug="grade-3",
        authorize=authorize if authorize is not None else _allow,
        **relay_kwargs,
    )
    return app


def test_forwards_completed_verbatim_with_bearer() -> None:
    from starlette.testclient import TestClient

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = request.content
        return httpx.Response(200, json=COMPLETED_BODY)

    app = _make_fastapi_app(handler)
    with TestClient(app) as tc:
        resp = tc.post("/agent/execute", json={"messages": [{"role": "user", "content": "hi"}]})

    assert resp.status_code == 200
    assert resp.json() == COMPLETED_BODY
    # Forwarded to the pinned flow's /execute with the nk_ bearer injected.
    assert captured["path"] == "/api/v1/seq/acme/spelling/grade-3/execute"
    assert captured["auth"] == "Bearer nk_test"


def test_relays_paused_verbatim() -> None:
    from starlette.testclient import TestClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=PAUSED_BODY)

    app = _make_fastapi_app(handler)
    with TestClient(app) as tc:
        resp = tc.post("/agent/execute", json={"messages": [{"role": "user", "content": "hi"}]})

    assert resp.status_code == 200
    assert resp.json() == PAUSED_BODY


@pytest.mark.parametrize("status", [400, 402, 409, 413, 500, 503])
def test_relays_upstream_error_status_verbatim(status: int) -> None:
    """A verbatim relay passes 4xx/5xx through (needs raise_for_status=False)."""
    from starlette.testclient import TestClient

    err_body = {"detail": {"code": "TOOLS_NOT_ENABLED", "message": "no tools"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=err_body)

    app = _make_fastapi_app(handler)
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post("/agent/execute", json={"message": "hi"})

    assert resp.status_code == status
    assert resp.json() == err_body


def test_non_json_upstream_normalized() -> None:
    from starlette.testclient import TestClient

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"<html>Bad Gateway</html>")

    app = _make_fastapi_app(handler)
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post("/agent/execute", json={"message": "hi"})

    assert resp.status_code == 502
    assert resp.json() == {"detail": "UPSTREAM_NON_JSON"}


def test_body_too_large_before_forward() -> None:
    from starlette.testclient import TestClient

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=COMPLETED_BODY)

    app = _make_fastapi_app(handler, max_body_bytes=32)
    with TestClient(app) as tc:
        resp = tc.post("/agent/execute", json={"message": "x" * 500})

    assert resp.status_code == 413
    assert resp.json() == {"detail": "BODY_TOO_LARGE"}
    assert calls["n"] == 0  # never forwarded upstream


def test_too_many_messages_before_forward() -> None:
    from starlette.testclient import TestClient

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=COMPLETED_BODY)

    app = _make_fastapi_app(handler, max_messages=2)
    with TestClient(app) as tc:
        resp = tc.post(
            "/agent/execute",
            json={"messages": [{"role": "user", "content": str(i)} for i in range(5)]},
        )

    assert resp.status_code == 413
    assert resp.json() == {"detail": "TOO_MANY_MESSAGES"}
    assert calls["n"] == 0


def test_invalid_json_before_forward() -> None:
    from starlette.testclient import TestClient

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=COMPLETED_BODY)

    app = _make_fastapi_app(handler)
    with TestClient(app) as tc:
        resp = tc.post(
            "/agent/execute",
            content=b"{not valid json",
            headers={"content-type": "application/json"},
        )

    assert resp.status_code == 400
    assert resp.json() == {"detail": "INVALID_JSON"}
    assert calls["n"] == 0


def test_connection_error_returns_502() -> None:
    from starlette.testclient import TestClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down")

    app = _make_fastapi_app(handler)
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post("/agent/execute", json={"message": "hi"})

    assert resp.status_code == 502
    assert resp.json() == {"detail": "UPSTREAM_UNAVAILABLE"}


def test_authorize_rejection_blocks_forward() -> None:
    from fastapi import HTTPException
    from starlette.testclient import TestClient

    calls = {"n": 0}
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=COMPLETED_BODY)

    async def deny(request: Any) -> None:
        seen["called"] = True
        raise HTTPException(status_code=403, detail="not a maker")

    app = _make_fastapi_app(handler, authorize=deny)
    with TestClient(app, raise_server_exceptions=False) as tc:
        resp = tc.post("/agent/execute", json={"message": "hi"})

    assert resp.status_code == 403
    assert resp.json()["detail"] == "not a maker"
    assert seen.get("called") is True
    assert calls["n"] == 0  # authorize rejected before any forward


# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------


def _make_flask_app(handler: Any, *, authorize: Any = None, **bp_kwargs: Any):
    pytest.importorskip("flask")
    from flask import Flask

    client = Noukai(api_key="nk_test", env="dev")
    client._transport._httpx_client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=client._transport._base_url
    )

    def _allow(_request: Any) -> None:
        return None

    app = Flask(__name__)
    app.register_blueprint(
        flow_relay_blueprint(
            client=client,
            org="acme",
            project="spelling",
            slug="grade-3",
            authorize=authorize if authorize is not None else _allow,
            **bp_kwargs,
        )
    )
    return app


def test_flask_forwards_completed_verbatim_with_bearer() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=COMPLETED_BODY)

    app = _make_flask_app(handler)
    resp = app.test_client().post("/agent/execute", json={"message": "hi"})

    assert resp.status_code == 200
    assert resp.get_json() == COMPLETED_BODY
    assert captured["path"] == "/api/v1/seq/acme/spelling/grade-3/execute"
    assert captured["auth"] == "Bearer nk_test"


def test_flask_relays_upstream_error_verbatim() -> None:
    err_body = {"detail": {"code": "INSUFFICIENT_CREDITS", "message": "broke"}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json=err_body)

    app = _make_flask_app(handler)
    resp = app.test_client().post("/agent/execute", json={"message": "hi"})

    assert resp.status_code == 402
    assert resp.get_json() == err_body


def test_flask_body_too_large_via_bounds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=COMPLETED_BODY)

    app = _make_flask_app(handler, bounds=RelayBounds(max_body_bytes=16))
    resp = app.test_client().post("/agent/execute", json={"message": "x" * 500})

    assert resp.status_code == 413
    assert resp.get_json() == {"detail": "BODY_TOO_LARGE"}
    assert calls["n"] == 0


def test_flask_authorize_rejection_blocks_forward() -> None:
    from werkzeug.exceptions import Forbidden

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=COMPLETED_BODY)

    def deny(_request: Any) -> None:
        raise Forbidden("not a maker")

    app = _make_flask_app(handler, authorize=deny)
    resp = app.test_client().post("/agent/execute", json={"message": "hi"})

    assert resp.status_code == 403
    assert calls["n"] == 0


def test_flask_non_json_upstream_normalized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, content=b"<html>Bad Gateway</html>")

    app = _make_flask_app(handler)
    resp = app.test_client().post("/agent/execute", json={"message": "hi"})

    assert resp.status_code == 502
    assert resp.get_json() == {"detail": "UPSTREAM_NON_JSON"}


def test_flask_too_many_messages_before_forward() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=COMPLETED_BODY)

    app = _make_flask_app(handler, bounds=RelayBounds(max_messages=2))
    resp = app.test_client().post(
        "/agent/execute",
        json={"messages": [{"role": "user", "content": str(i)} for i in range(5)]},
    )

    assert resp.status_code == 413
    assert resp.get_json() == {"detail": "TOO_MANY_MESSAGES"}
    assert calls["n"] == 0


def test_flask_connection_error_returns_502() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("upstream down")

    app = _make_flask_app(handler)
    resp = app.test_client().post("/agent/execute", json={"message": "hi"})

    assert resp.status_code == 502
    assert resp.get_json() == {"detail": "UPSTREAM_UNAVAILABLE"}
