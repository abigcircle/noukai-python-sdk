"""Non-raising transport mode (design 20260903-SDK-agent-relay, PR-1).

``request(..., raise_for_status=False)`` returns the ``Response`` on non-2xx
instead of raising a typed exception. This is the primitive the relay adapter
relies on to pass 4xx/5xx from the upstream ``/execute`` back to the browser
verbatim. Default (``raise_for_status=True``) preserves today's behavior — the
existing ``test_transport.py`` suite is the regression guard for that.

Covers both ``AsyncTransport`` and ``SyncTransport`` so async/sync parity holds.
"""

import asyncio

import httpx
import pytest

from noukai_sdk._errors import APIConnectionError, FlowExecutionError, FlowNotFoundError
from noukai_sdk._transport import AsyncTransport, SyncTransport


def make_async_transport(handler, **kwargs):
    defaults = dict(
        api_key="nk_test123", base_url="https://noukai.dev/api/v1", timeout=30.0, max_retries=1
    )
    defaults.update(kwargs)
    transport = AsyncTransport(**defaults)
    transport._httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=defaults["base_url"]
    )
    return transport


def make_sync_transport(handler, **kwargs):
    defaults = dict(
        api_key="nk_test123", base_url="https://noukai.dev/api/v1", timeout=30.0, max_retries=1
    )
    defaults.update(kwargs)
    transport = SyncTransport(**defaults)
    transport._httpx_client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url=defaults["base_url"]
    )
    return transport


class TestAsyncNonRaising:
    @pytest.mark.asyncio
    async def test_non_2xx_returns_response_instead_of_raising(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404, json={"detail": {"code": "FLOW_NOT_FOUND", "message": "nope"}}
            )

        transport = make_async_transport(handler)
        resp = await transport.request(
            "POST", "/execute", json={"message": "hi"}, raise_for_status=False
        )
        await transport.aclose()
        assert resp.status_code == 404
        assert resp.body == {"detail": {"code": "FLOW_NOT_FOUND", "message": "nope"}}

    @pytest.mark.asyncio
    async def test_5xx_returned_verbatim_when_not_raising(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "down"})

        transport = make_async_transport(handler)
        resp = await transport.request("POST", "/execute", json={"m": 1}, raise_for_status=False)
        await transport.aclose()
        assert resp.status_code == 503
        assert resp.body == {"detail": "down"}

    @pytest.mark.asyncio
    async def test_default_still_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": {"code": "FLOW_NOT_FOUND", "message": "no"}})

        transport = make_async_transport(handler)
        with pytest.raises(FlowNotFoundError):
            await transport.request("POST", "/execute", json={"m": 1})
        await transport.aclose()

    @pytest.mark.asyncio
    async def test_2xx_unaffected_by_flag(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        transport = make_async_transport(handler)
        resp = await transport.request("POST", "/execute", json={"m": 1}, raise_for_status=False)
        await transport.aclose()
        assert resp.status_code == 200
        assert resp.body == {"ok": True}

    @pytest.mark.asyncio
    async def test_non_json_body_returned_as_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(502, content=b"<html>Bad Gateway</html>")

        transport = make_async_transport(handler)
        resp = await transport.request("POST", "/execute", json={"m": 1}, raise_for_status=False)
        await transport.aclose()
        assert resp.status_code == 502
        assert resp.body is None

    @pytest.mark.asyncio
    async def test_retries_before_non_raising_return(self, monkeypatch) -> None:
        """A retryable status on an idempotent method is still retried before the
        non-raising early return kicks in."""

        async def fake_sleep(_s: float) -> None:
            return None

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(503, json={"detail": "down"})
            return httpx.Response(200, json={"ok": True})

        transport = make_async_transport(handler, max_retries=1)
        resp = await transport.request("GET", "/x", raise_for_status=False)
        await transport.aclose()
        assert len(attempts) == 2  # retried
        assert resp.status_code == 200
        assert resp.body == {"ok": True}

    @pytest.mark.asyncio
    async def test_connection_error_still_raises_in_non_raising_mode(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("DNS boom")

        transport = make_async_transport(handler)
        with pytest.raises(APIConnectionError):
            await transport.request("POST", "/execute", json={"m": 1}, raise_for_status=False)
        await transport.aclose()


class TestSyncNonRaising:
    def test_non_2xx_returns_response_instead_of_raising(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                500, json={"detail": {"code": "INTERNAL_ERROR", "message": "boom"}}
            )

        transport = make_sync_transport(handler)
        resp = transport.request("POST", "/execute", json={"m": 1}, raise_for_status=False)
        transport.close()
        assert resp.status_code == 500
        assert resp.body == {"detail": {"code": "INTERNAL_ERROR", "message": "boom"}}

    def test_default_still_raises(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"detail": "boom"})

        transport = make_sync_transport(handler)
        with pytest.raises(FlowExecutionError):
            transport.request("POST", "/execute", json={"m": 1})
        transport.close()

    def test_non_json_body_returned_as_none(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(413, content=b"too big")

        transport = make_sync_transport(handler)
        resp = transport.request("POST", "/execute", json={"m": 1}, raise_for_status=False)
        transport.close()
        assert resp.status_code == 413
        assert resp.body is None
