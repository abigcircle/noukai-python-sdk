"""Transport contract tests. All HTTP calls are mocked via httpx.MockTransport.

These tests do NOT verify endpoint shapes (Phase 4+ does). They verify that
the transport correctly:
  - attaches auth + version + UA headers
  - resolves the base URL
  - retries 5xx exactly N times with backoff
  - maps status codes to typed exceptions
  - captures X-Request-ID into the exception
  - invokes the log_handler at request and response
"""

import asyncio
import json

import httpx
import pytest

from noukai_sdk._errors import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    FlowExecutionError,
    FlowNotFoundError,
    InsufficientCreditsError,
    PermissionDeniedError,
    RateLimitError,
)
from noukai_sdk._transport import AsyncTransport


def make_transport(handler, **kwargs):
    """Build an AsyncTransport whose httpx layer is mocked by `handler`."""
    defaults = dict(
        api_key="nk_test123",
        base_url="https://noukai.xyz/api/v1",
        timeout=30.0,
        max_retries=1,
    )
    defaults.update(kwargs)
    transport = AsyncTransport(**defaults)
    # Inject httpx.MockTransport — Phase 3 implementation must expose a
    # hook (private attribute or constructor arg) to swap the transport.
    transport._httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=defaults["base_url"],
    )
    return transport


class TestHeaders:
    @pytest.mark.asyncio
    async def test_authorization_header_set(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"status": "ok"})

        transport = make_transport(handler)
        await transport.request("GET", "/health")
        await transport.aclose()
        assert captured["auth"] == "Bearer nk_test123"

    @pytest.mark.asyncio
    async def test_api_version_header_set(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["v"] = request.headers.get("X-Noukai-API-Version")
            return httpx.Response(200, json={})

        transport = make_transport(handler)
        await transport.request("GET", "/health")
        await transport.aclose()
        assert captured["v"] == "2026-05-31"  # from _constants.API_VERSION

    @pytest.mark.asyncio
    async def test_user_agent_includes_sdk_version(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["ua"] = request.headers.get("User-Agent")
            return httpx.Response(200, json={})

        transport = make_transport(handler)
        await transport.request("GET", "/health")
        await transport.aclose()
        from noukai_sdk._version import __version__

        assert f"noukai-python/{__version__}" in captured["ua"]
        assert "httpx" in captured["ua"]


class TestRequestId:
    @pytest.mark.asyncio
    async def test_captures_request_id_from_response_header(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"x": 1},
                headers={"X-Request-ID": "req-abc-123"},
            )

        transport = make_transport(handler)
        resp = await transport.request("GET", "/x")
        await transport.aclose()
        assert resp.request_id == "req-abc-123"

    @pytest.mark.asyncio
    async def test_request_id_propagated_into_exception(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                404,
                json={"detail": {"code": "FLOW_NOT_FOUND", "message": "nope"}},
                headers={"X-Request-ID": "req-xyz"},
            )

        transport = make_transport(handler)
        with pytest.raises(FlowNotFoundError) as exc_info:
            await transport.request("GET", "/x")
        await transport.aclose()
        assert exc_info.value.request_id == "req-xyz"


class TestRetries:
    @pytest.mark.asyncio
    async def test_5xx_retried_once_by_default_on_idempotent_get(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(503, json={"detail": "down"})
            return httpx.Response(200, json={"ok": True})

        transport = make_transport(handler, max_retries=1)
        resp = await transport.request("GET", "/x")
        await transport.aclose()
        assert len(attempts) == 2
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_4xx_not_retried(self):
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(404, json={"detail": "nope"})

        transport = make_transport(handler)
        with pytest.raises(FlowNotFoundError):
            await transport.request("GET", "/x")
        await transport.aclose()
        assert len(attempts) == 1  # no retry

    @pytest.mark.asyncio
    async def test_retries_exhausted_raises_flow_execution_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "down"})

        transport = make_transport(handler, max_retries=2)
        with pytest.raises(FlowExecutionError) as exc_info:
            await transport.request("GET", "/x")
        await transport.aclose()
        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_exponential_backoff_timing(self, monkeypatch):
        """Backoff sequence: 1s, 4s, 16s (4^n). Verify via mocked sleep."""
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        # Pin jitter to 1.0 so the test verifies the base backoff curve.
        monkeypatch.setattr("noukai_sdk._transport_shared.random.uniform", lambda lo, hi: 1.0)
        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"detail": "down"})

        transport = make_transport(handler, max_retries=3)
        with pytest.raises(FlowExecutionError):
            await transport.request("GET", "/x")
        await transport.aclose()
        assert sleeps == [1.0, 4.0, 16.0]

    @pytest.mark.asyncio
    async def test_post_not_retried_on_5xx_by_default(self):
        """POST is non-idempotent by default — 5xx must not silently retry."""
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(503, json={"detail": "down"})

        transport = make_transport(handler, max_retries=3)
        with pytest.raises(FlowExecutionError):
            await transport.request("POST", "/execute", json={"message": "hi"})
        await transport.aclose()
        assert len(attempts) == 1  # zero retries on POST

    @pytest.mark.asyncio
    async def test_post_retries_when_explicitly_idempotent(self):
        """Callers can opt POST into the retry policy by passing idempotent=True."""
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(503, json={"detail": "down"})
            return httpx.Response(200, json={"ok": True})

        transport = make_transport(handler, max_retries=1)
        resp = await transport.request("POST", "/x", json={"k": "v"}, idempotent=True)
        await transport.aclose()
        assert len(attempts) == 2
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_408_retried_on_get(self):
        """408 Request Timeout is conventionally retryable."""
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(408, json={"detail": "timeout"})
            return httpx.Response(200, json={"ok": True})

        transport = make_transport(handler, max_retries=1)
        resp = await transport.request("GET", "/x")
        await transport.aclose()
        assert len(attempts) == 2
        assert resp.status_code == 200


class TestExceptionMapping:
    @pytest.mark.parametrize(
        ("status", "error_code", "exc_class"),
        [
            (401, "UNAUTHENTICATED", AuthenticationError),
            (402, "INSUFFICIENT_CREDITS", InsufficientCreditsError),
            (402, "CREDITS_EXHAUSTED", InsufficientCreditsError),
            (403, "FORBIDDEN", PermissionDeniedError),
            (404, "FLOW_NOT_FOUND", FlowNotFoundError),
            (429, "RATE_LIMIT", RateLimitError),
            (500, "INTERNAL_ERROR", FlowExecutionError),
            (502, "BYOK_KEY_REJECTED", FlowExecutionError),
        ],
    )
    @pytest.mark.asyncio
    async def test_status_to_exception(self, status, error_code, exc_class):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status,
                json={"detail": {"code": error_code, "message": "x"}},
            )

        transport = make_transport(handler)
        with pytest.raises(exc_class) as exc_info:
            await transport.request("GET", "/x")
        await transport.aclose()
        assert exc_info.value.status_code == status
        assert exc_info.value.code == error_code

    @pytest.mark.asyncio
    async def test_401_captures_www_authenticate(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                401,
                json={"detail": "invalid key"},
                headers={
                    "WWW-Authenticate": (
                        'Bearer error="invalid_token", error_description="token expired"'
                    )
                },
            )

        transport = make_transport(handler)
        with pytest.raises(AuthenticationError) as exc_info:
            await transport.request("GET", "/x")
        await transport.aclose()
        assert "invalid_token" in (exc_info.value.www_authenticate or "")

    @pytest.mark.asyncio
    async def test_rate_limit_captures_retry_after(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                429,
                json={"detail": "slow"},
                headers={"Retry-After": "5"},
            )

        transport = make_transport(handler)
        with pytest.raises(RateLimitError) as exc_info:
            await transport.request("GET", "/x")
        await transport.aclose()
        assert exc_info.value.retry_after == 5.0


class TestConnectionErrors:
    @pytest.mark.asyncio
    async def test_dns_failure_maps_to_api_connection_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("DNS resolution failed")

        transport = make_transport(handler)
        with pytest.raises(APIConnectionError):
            await transport.request("GET", "/x")
        await transport.aclose()

    @pytest.mark.asyncio
    async def test_timeout_maps_to_api_timeout_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("read timeout")

        transport = make_transport(handler)
        with pytest.raises(APITimeoutError):
            await transport.request("GET", "/x")
        await transport.aclose()


class TestLogHandler:
    @pytest.mark.asyncio
    async def test_log_handler_invoked_on_request_and_response(self):
        events = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"x": 1}, headers={"X-Request-ID": "r1"})

        transport = make_transport(handler, log_handler=events.append)
        await transport.request("GET", "/x")
        await transport.aclose()
        phases = [e["phase"] for e in events]
        assert "request" in phases
        assert "response" in phases
        resp_event = next(e for e in events if e["phase"] == "response")
        assert resp_event["status_code"] == 200
        assert resp_event["request_id"] == "r1"

    @pytest.mark.asyncio
    async def test_log_handler_omits_payloads_by_default(self):
        events = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"secret": "data"})

        transport = make_transport(handler, log_handler=events.append)
        await transport.request("POST", "/x", json={"input": "secret-input"})
        await transport.aclose()
        for e in events:
            assert "secret" not in json.dumps(e)
            assert "request_body" not in e
            assert "response_body" not in e

    @pytest.mark.asyncio
    async def test_log_handler_includes_payloads_when_opted_in(self):
        events = []

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"x": 1})

        transport = make_transport(
            handler,
            log_handler=events.append,
            log_payloads=True,
        )
        await transport.request("POST", "/x", json={"input": "hello"})
        await transport.aclose()
        bodies = [e.get("request_body") for e in events if e.get("request_body")]
        assert bodies == [{"input": "hello"}]


class TestUrlResolution:
    @pytest.mark.asyncio
    async def test_path_prepended_with_base_url(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={})

        transport = make_transport(handler, base_url="https://noukai.xyz/api/v1")
        await transport.request("GET", "/seq/acme/spelling/grade-3/execute")
        await transport.aclose()
        assert captured["url"] == ("https://noukai.xyz/api/v1/seq/acme/spelling/grade-3/execute")

    @pytest.mark.asyncio
    async def test_double_slash_avoided(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={})

        transport = make_transport(
            handler,
            base_url="https://noukai.xyz/api/v1/",
        )
        await transport.request("GET", "/health")
        await transport.aclose()
        assert "//health" not in captured["url"]


class TestExtraHeadersReservedGuard:
    """`extra_headers=` must not let a caller overwrite transport-managed
    headers — protects against accidental bearer-token rotation by a
    misconfigured replay scope."""

    @pytest.mark.asyncio
    async def test_non_reserved_header_passes_through(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update({k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(200, json={})

        transport = make_transport(handler)
        await transport.request("GET", "/x", extra_headers={"X-Session-Id": "sess-1"})
        await transport.aclose()
        assert captured.get("x-session-id") == "sess-1"

    @pytest.mark.asyncio
    async def test_authorization_cannot_be_overridden(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization", "")
            return httpx.Response(200, json={})

        transport = make_transport(handler, api_key="nk_real")
        await transport.request(
            "GET",
            "/x",
            extra_headers={"Authorization": "Bearer nk_evil", "AUTHORIZATION": "Bearer nk_evil2"},
        )
        await transport.aclose()
        assert captured["authorization"] == "Bearer nk_real"

    @pytest.mark.asyncio
    async def test_api_version_user_agent_cookie_dropped(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update({k.lower(): v for k, v in request.headers.items()})
            return httpx.Response(200, json={})

        transport = make_transport(handler)
        await transport.request(
            "GET",
            "/x",
            extra_headers={
                "X-Noukai-API-Version": "1900-01-01",
                "User-Agent": "evil/1.0",
                "Cookie": "session=stolen",
            },
        )
        await transport.aclose()
        assert captured.get("x-noukai-api-version") != "1900-01-01"
        assert "evil/1.0" not in captured.get("user-agent", "")
        # Cookie reserved -> not forwarded
        assert "session=stolen" not in captured.get("cookie", "")

    @pytest.mark.asyncio
    async def test_guard_also_applies_to_stream(self) -> None:
        captured: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization", "")
            return httpx.Response(200, content=b"")

        transport = make_transport(handler, api_key="nk_real")
        agen = transport.stream(
            "POST",
            "/stream",
            extra_headers={"Authorization": "Bearer nk_evil"},
        )
        async for _ in agen:
            pass
        await transport.aclose()
        assert captured["authorization"] == "Bearer nk_real"
