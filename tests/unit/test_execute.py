"""Phase 4: AsyncFlow.execute — URL routing, request body, response parsing."""

import httpx
import pytest

from noukai_sdk import AsyncNoukai, ExecuteResult, PausedResult


def make_client_with_handler(handler):
    client = AsyncNoukai(api_key="nk_test")
    client._transport._httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=client._transport._base_url,
    )
    return client


class TestExecuteUrl:
    @pytest.mark.asyncio
    async def test_draft_version_uses_unversioned_url(self):
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "result": {"x": 1},
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        client = make_client_with_handler(handler)
        flow = client.flow("acme/spelling/grade-3")
        await flow.execute(message="hi")
        await client.aclose()
        assert captured["url"].endswith("/seq/acme/spelling/grade-3/execute")
        assert "/v" not in captured["url"].split("/seq")[1]

    @pytest.mark.asyncio
    async def test_versioned_int_uses_v_n_url(self):
        captured = {}

        def handler(request):
            captured["url"] = str(request.url)
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        client = make_client_with_handler(handler)
        flow = client.flow("acme/spelling/grade-3")
        await flow.execute(message="hi", version=3)
        await client.aclose()
        assert captured["url"].endswith("/seq/acme/spelling/grade-3/v3/execute")

    @pytest.mark.asyncio
    async def test_production_version_raises_not_implemented_error_async(self):
        """version="production" must raise NotImplementedError before any HTTP
        call is made (server contract not yet finalised)."""
        client = AsyncNoukai(api_key="nk_test")
        flow = client.flow("acme/spelling/grade-3")
        with pytest.raises(NotImplementedError, match="production"):
            await flow.execute(message="hi", version="production")
        await client.aclose()

    def test_production_version_raises_not_implemented_error_sync(self):
        """Sync client: version="production" must raise NotImplementedError."""
        from noukai_sdk import Noukai

        client = Noukai(api_key="nk_test")
        flow = client.flow("acme/spelling/grade-3")
        with pytest.raises(NotImplementedError, match="production"):
            flow.execute(message="hi", version="production")


class TestExecuteRequestBody:
    @pytest.mark.asyncio
    async def test_message_and_parameters_in_body(self):
        captured = {}

        def handler(request):
            captured["body"] = request.read()
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        client = make_client_with_handler(handler)
        flow = client.flow("a/b/c")
        await flow.execute(message="hi", parameters={"k": "v"})
        await client.aclose()
        import json

        body = json.loads(captured["body"])
        assert body["message"] == "hi"
        assert body["parameters"] == {"k": "v"}

    @pytest.mark.asyncio
    async def test_block_overrides_camelcased(self):
        captured = {}

        def handler(request):
            captured["body"] = request.read()
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        client = make_client_with_handler(handler)
        flow = client.flow("a/b/c")
        await flow.execute(
            message="hi",
            block_overrides={"step-1": {"model": "anthropic/claude-haiku-4-5"}},
        )
        await client.aclose()
        import json

        body = json.loads(captured["body"])
        assert "blockOverrides" in body
        assert body["blockOverrides"]["step-1"]["model"] == "anthropic/claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_trace_flag_default_false(self):
        captured = {}

        def handler(request):
            captured["body"] = request.read()
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        client = make_client_with_handler(handler)
        flow = client.flow("a/b/c")
        await flow.execute(message="hi")
        await client.aclose()
        import json

        body = json.loads(captured["body"])
        assert body.get("trace") is False or "trace" not in body


class TestExecuteResponseParsing:
    @pytest.mark.asyncio
    async def test_completed_returns_execute_result(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "result": {"answer": 42},
                    "flowId": "f-xyz",
                    "blockCount": 3,
                },
            )

        client = make_client_with_handler(handler)
        result = await client.flow("a/b/c").execute(message="hi")
        await client.aclose()
        assert isinstance(result, ExecuteResult)
        assert result.output == {"answer": 42}
        assert result.flow_id == "f-xyz"
        assert result.requires_tool_calls is False

    @pytest.mark.asyncio
    async def test_paused_returns_paused_result(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "status": "tool_calls_required",
                    "executionId": "exec-1",
                    "pausedAtStep": "step-1",
                    "iterationsUsed": 1,
                    "toolCallMessages": [{"role": "assistant"}],
                    "toolCalls": [{"id": "tc-1", "function": {"name": "search"}}],
                    "accumulatedOutputs": {},
                    "flowId": "f",
                    "blockCount": 2,
                },
            )

        client = make_client_with_handler(handler)
        result = await client.flow("a/b/c").execute(message="hi", tools=[{}])
        await client.aclose()
        assert isinstance(result, PausedResult)
        assert result.requires_tool_calls is True
        assert result.tool_calls[0]["function"]["name"] == "search"
