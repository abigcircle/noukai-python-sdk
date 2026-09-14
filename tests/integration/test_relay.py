"""Integration tests: the agent-over-relay round-trip (design 20260903-SDK-agent-relay).

A keyless ``RelayFlow`` drives a REAL relay adapter mounted in-process, which
forwards (with the ``nk_`` key injected) to the live Noukai server and relays the
response back verbatim. Both transports are exercised for parity:

- **async** — FastAPI :func:`mount_flow_relay` + :class:`AsyncNoukai`, driven by a
  keyless :class:`AsyncRelayFlow` over an in-process ``httpx.ASGITransport``.
- **sync** — Flask :func:`flow_relay_blueprint` + :class:`Noukai`, driven by a
  keyless :class:`RelayFlow` over an in-process ``httpx.WSGITransport``.

The in-process transport is the client→relay leg (keyless); the relay→server leg
is a real HTTP call to local dev. This is the true end-to-end path a browser/agent
uses, minus a socket. Covers: completion, pause→resume *through the relay*,
``authorize`` rejection (403 verbatim), and the raw-byte body bound (413).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from noukai_sdk import (
    AsyncNoukai,
    AsyncRelayFlow,
    ExecuteResult,
    Noukai,
    NoukaiError,
    PausedResult,
    RelayFlow,
)
from noukai_sdk.adapters.relay import RelayBounds, flow_relay_blueprint, mount_flow_relay

_RELAY_PATH = "/agent/execute"
_RELAY_URL = f"http://relay{_RELAY_PATH}"

_GET_WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get the current weather for a location (integration test stub).",
        "parameters": {
            "type": "object",
            "properties": {"location": {"type": "string", "description": "City name"}},
            "required": ["location"],
        },
    },
}


def _echo_handler(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"role": "tool", "toolCallId": call.get("id", ""), "content": "sunny, 22C"}
        for call in tool_calls
    ]


# ---------------------------------------------------------------------------
# async harness — FastAPI relay + AsyncNoukai, driven in-process via ASGITransport
# ---------------------------------------------------------------------------


def _make_fastapi_app(
    client: AsyncNoukai,
    org: str,
    project: str,
    slug: str,
    *,
    authorize: Any,
    max_body_bytes: int = 262144,
) -> Any:
    from fastapi import FastAPI

    app = FastAPI()
    mount_flow_relay(
        app,
        client=client,
        org=org,
        project=project,
        slug=slug,
        path=_RELAY_PATH,
        authorize=authorize,
        max_body_bytes=max_body_bytes,
    )
    return app


async def _allow(request: Any) -> None:
    return None


def _asgi_client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://relay")


@pytest.mark.integration
async def test_relay_async_completes(agent_coords: tuple[str, str, str, str]) -> None:
    key, org, project, slug = agent_coords
    client = AsyncNoukai(api_key=key, org=org, project=project)
    try:
        app = _make_fastapi_app(client, org, project, slug, authorize=_allow)
        async with _asgi_client(app) as hc:
            flow = AsyncRelayFlow(_RELAY_URL, client=hc)
            result = await flow.execute(
                messages=[{"role": "user", "content": "What is the weather in Tokyo?"}],
                tools=[_GET_WEATHER],
                tool_handler=_echo_handler,
            )
        assert isinstance(result, ExecuteResult)
        assert result.status == "completed"
    finally:
        await client.aclose()


@pytest.mark.integration
async def test_relay_async_pause_resume_through_relay(
    agent_coords: tuple[str, str, str, str],
) -> None:
    """No handler → PausedResult; resuming it routes back THROUGH the relay
    (the paused result carries the same keyless relay transport)."""
    key, org, project, slug = agent_coords
    client = AsyncNoukai(api_key=key, org=org, project=project)
    try:
        app = _make_fastapi_app(client, org, project, slug, authorize=_allow)
        async with _asgi_client(app) as hc:
            flow = AsyncRelayFlow(_RELAY_URL, client=hc)
            paused = await flow.execute(
                messages=[{"role": "user", "content": "What is the weather in Paris?"}],
                tools=[_GET_WEATHER],
            )
            if isinstance(paused, ExecuteResult):
                pytest.skip(
                    "Server completed without pausing — verify the agent fixture tool config."
                )
            assert isinstance(paused, PausedResult)
            assert len(paused.tool_calls) >= 1
            final = await paused.resume(tool_results=_echo_handler(paused.tool_calls))
        assert isinstance(final, (ExecuteResult, PausedResult))
    finally:
        await client.aclose()


@pytest.mark.integration
async def test_relay_authorize_rejection_raises(agent_coords: tuple[str, str, str, str]) -> None:
    """A raising ``authorize`` short-circuits before forwarding; the relay's 403
    is relayed verbatim and surfaces as a typed SDK error."""
    from fastapi import HTTPException

    async def deny(request: Any) -> None:
        raise HTTPException(status_code=403, detail="FORBIDDEN")

    key, org, project, slug = agent_coords
    client = AsyncNoukai(api_key=key, org=org, project=project)
    try:
        app = _make_fastapi_app(client, org, project, slug, authorize=deny)
        async with _asgi_client(app) as hc:
            flow = AsyncRelayFlow(_RELAY_URL, client=hc)
            with pytest.raises(NoukaiError) as excinfo:
                await flow.execute(
                    messages=[{"role": "user", "content": "hi"}],
                    tools=[_GET_WEATHER],
                    tool_handler=_echo_handler,
                )
        assert excinfo.value.status_code == 403
    finally:
        await client.aclose()


@pytest.mark.integration
async def test_relay_body_bound_raises(agent_coords: tuple[str, str, str, str]) -> None:
    """A raw-byte body over ``max_body_bytes`` is rejected (413) before the relay
    ever forwards upstream."""
    key, org, project, slug = agent_coords
    client = AsyncNoukai(api_key=key, org=org, project=project)
    try:
        app = _make_fastapi_app(client, org, project, slug, authorize=_allow, max_body_bytes=10)
        async with _asgi_client(app) as hc:
            flow = AsyncRelayFlow(_RELAY_URL, client=hc)
            with pytest.raises(NoukaiError) as excinfo:
                await flow.execute(
                    messages=[{"role": "user", "content": "x" * 500}],
                    tools=[_GET_WEATHER],
                    tool_handler=_echo_handler,
                )
        assert excinfo.value.status_code == 413
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# sync harness — Flask relay + Noukai, driven in-process via WSGITransport
# ---------------------------------------------------------------------------


def _make_flask_app(
    client: Noukai,
    org: str,
    project: str,
    slug: str,
    *,
    authorize: Any,
    bounds: RelayBounds | None = None,
) -> Any:
    from flask import Flask

    app = Flask(__name__)
    app.register_blueprint(
        flow_relay_blueprint(
            client=client,
            org=org,
            project=project,
            slug=slug,
            path=_RELAY_PATH,
            authorize=authorize,
            bounds=bounds,
        )
    )
    return app


def _flask_allow(request: Any) -> None:
    return None


def _wsgi_client(app: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.WSGITransport(app=app), base_url="http://relay")


@pytest.mark.integration
def test_relay_sync_completes(agent_coords: tuple[str, str, str, str]) -> None:
    key, org, project, slug = agent_coords
    with Noukai(api_key=key, org=org, project=project) as client:
        app = _make_flask_app(client, org, project, slug, authorize=_flask_allow)
        with _wsgi_client(app) as hc:
            flow = RelayFlow(_RELAY_URL, client=hc)
            result = flow.execute(
                messages=[{"role": "user", "content": "What is the weather in Tokyo?"}],
                tools=[_GET_WEATHER],
                tool_handler=_echo_handler,
            )
        assert isinstance(result, ExecuteResult)
        assert result.status == "completed"


@pytest.mark.integration
def test_relay_sync_pause_resume_through_relay(agent_coords: tuple[str, str, str, str]) -> None:
    key, org, project, slug = agent_coords
    with Noukai(api_key=key, org=org, project=project) as client:
        app = _make_flask_app(client, org, project, slug, authorize=_flask_allow)
        with _wsgi_client(app) as hc:
            flow = RelayFlow(_RELAY_URL, client=hc)
            paused = flow.execute(
                messages=[{"role": "user", "content": "What is the weather in Paris?"}],
                tools=[_GET_WEATHER],
            )
            if isinstance(paused, ExecuteResult):
                pytest.skip(
                    "Server completed without pausing — verify the agent fixture tool config."
                )
            assert isinstance(paused, PausedResult)
            assert len(paused.tool_calls) >= 1
            final = paused.resume_sync(tool_results=_echo_handler(paused.tool_calls))
        assert isinstance(final, (ExecuteResult, PausedResult))
