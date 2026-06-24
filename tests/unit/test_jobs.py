"""Phase 4: AsyncJob — execute_async, poll, wait with timeout."""

import asyncio
import time

import httpx
import pytest

from noukai_sdk import APITimeoutError, AsyncJob, AsyncNoukai, FlowNotFoundError, JobStatus, Noukai


def make_client(handler):
    client = AsyncNoukai(api_key="nk_test")
    client._transport._httpx_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url=client._transport._base_url,
    )
    return client


def make_sync_client(handler):
    client = Noukai(api_key="nk_test")
    client._transport._httpx_client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=client._transport._base_url,
    )
    return client


_NOT_FOUND_BODY = {"detail": {"code": "JOB_NOT_FOUND", "message": "Job not found"}}


class TestExecuteAsync:
    @pytest.mark.asyncio
    async def test_returns_job_with_execution_id(self):
        def handler(request):
            return httpx.Response(
                200,
                json={
                    "executionId": "exec-123",
                    "status": "started",
                    "flowId": "f",
                    "blockCount": 2,
                },
            )

        client = make_client(handler)
        job = await client.flow("a/b/c").execute_async(message="hi")
        await client.aclose()
        assert isinstance(job, AsyncJob)
        assert job.execution_id == "exec-123"
        assert job.flow_id == "f"

    @pytest.mark.asyncio
    async def test_posts_to_jobs_endpoint(self):
        captured = {}

        def handler(request):
            captured["path"] = request.url.path
            return httpx.Response(
                200,
                json={
                    "executionId": "e",
                    "status": "started",
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        client = make_client(handler)
        await client.flow("acme/spelling/grade-3").execute_async(message="hi")
        await client.aclose()
        assert captured["path"].endswith("/seq/acme/spelling/grade-3/jobs")


class TestJobPoll:
    @pytest.mark.asyncio
    async def test_poll_returns_status(self):
        def handler(request):
            if "jobs/exec-1" in request.url.path:
                return httpx.Response(
                    200,
                    json={
                        "executionId": "exec-1",
                        "status": "running",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "executionId": "exec-1",
                    "status": "started",
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        client = make_client(handler)
        job = await client.flow("a/b/c").execute_async(message="hi")
        status = await job.poll()
        await client.aclose()
        assert isinstance(status, JobStatus)
        assert status.status == "running"


class TestJobWait:
    @pytest.mark.asyncio
    async def test_returns_when_terminal(self, monkeypatch):
        calls = [0]

        def handler(request):
            if "jobs/" in request.url.path and request.method == "GET":
                calls[0] += 1
                if calls[0] < 3:
                    return httpx.Response(
                        200,
                        json={
                            "executionId": "e",
                            "status": "running",
                        },
                    )
                return httpx.Response(
                    200,
                    json={
                        "executionId": "e",
                        "status": "completed",
                        "result": {"answer": 42},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "executionId": "e",
                    "status": "started",
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        # Skip actual sleeping
        async def fake_sleep(_):
            pass

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        client = make_client(handler)
        job = await client.flow("a/b/c").execute_async(message="hi")
        final = await job.wait(timeout=10, poll_interval=0.1)
        await client.aclose()
        assert final.status == "completed"
        assert final.result == {"answer": 42}

    @pytest.mark.asyncio
    async def test_timeout_raises(self, monkeypatch):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "executionId": "e",
                        "status": "running",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "executionId": "e",
                    "status": "started",
                    "flowId": "f",
                    "blockCount": 1,
                },
            )

        async def fake_sleep(s):
            # Advance virtual clock by `s`
            pass

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        # Need to advance loop's time perception too; simplest: mock time.monotonic
        import time

        ticks = [0.0]

        def fake_monotonic():
            ticks[0] += 0.5
            return ticks[0]

        monkeypatch.setattr(time, "monotonic", fake_monotonic)

        client = make_client(handler)
        job = await client.flow("a/b/c").execute_async(message="hi")
        with pytest.raises(APITimeoutError):
            await job.wait(timeout=1.0, poll_interval=0.1)
        await client.aclose()


class TestJobGraceWindow:
    """The 5-second grace window after submission during which a 404 from the
    status endpoint is treated as "job not yet registered" rather than a real
    not-found. Covers the race where execute_async returns the executionId
    before the orchestrator-worker has inserted the flow_runs row.
    """

    @pytest.mark.asyncio
    async def test_async_404_within_grace_resumes_polling(self, monkeypatch):
        call_count = [0]

        def handler(request):
            if "jobs/" in request.url.path and request.method == "GET":
                call_count[0] += 1
                if call_count[0] == 1:
                    return httpx.Response(404, json=_NOT_FOUND_BODY)
                return httpx.Response(
                    200, json={"executionId": "e", "status": "completed", "result": {"ok": True}}
                )
            return httpx.Response(
                200,
                json={"executionId": "e", "status": "started", "flowId": "f", "blockCount": 1},
            )

        async def fake_sleep(_):
            pass

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        client = make_client(handler)
        job = await client.flow("a/b/c").execute_async(message="hi")
        final = await job.wait(timeout=10.0, poll_interval=0.1)
        await client.aclose()
        assert final.status == "completed"
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_async_404_after_grace_propagates(self, monkeypatch):
        def handler(request):
            if "jobs/" in request.url.path and request.method == "GET":
                return httpx.Response(404, json=_NOT_FOUND_BODY)
            return httpx.Response(
                200,
                json={"executionId": "e", "status": "started", "flowId": "f", "blockCount": 1},
            )

        async def fake_sleep(_):
            pass

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        client = make_client(handler)
        job = await client.flow("a/b/c").execute_async(message="hi")
        # Backdate submitted_at past the 5s grace window so the first poll's
        # 404 must propagate as FlowNotFoundError, not get suppressed.
        job._submitted_at = time.monotonic() - 10.0
        with pytest.raises(FlowNotFoundError):
            await job.wait(timeout=10.0, poll_interval=0.1)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_async_timeout_fires_even_during_grace(self, monkeypatch):
        # Mirrors the failing Node integration test: a 1ms timeout must throw
        # APITimeoutError even when every poll is returning a 404 that the
        # grace window would otherwise swallow.
        def handler(request):
            if "jobs/" in request.url.path and request.method == "GET":
                return httpx.Response(404, json=_NOT_FOUND_BODY)
            return httpx.Response(
                200,
                json={"executionId": "e", "status": "started", "flowId": "f", "blockCount": 1},
            )

        async def fake_sleep(_):
            pass

        monkeypatch.setattr(asyncio, "sleep", fake_sleep)

        # Advance monotonic clock 0.5s per call — same pattern as test_timeout_raises.
        ticks = [0.0]

        def fake_monotonic():
            ticks[0] += 0.5
            return ticks[0]

        monkeypatch.setattr(time, "monotonic", fake_monotonic)

        client = make_client(handler)
        job = await client.flow("a/b/c").execute_async(message="hi")
        with pytest.raises(APITimeoutError):
            await job.wait(timeout=0.001, poll_interval=0.1)
        await client.aclose()

    def test_sync_404_within_grace_resumes_polling(self, monkeypatch):
        call_count = [0]

        def handler(request):
            if "jobs/" in request.url.path and request.method == "GET":
                call_count[0] += 1
                if call_count[0] == 1:
                    return httpx.Response(404, json=_NOT_FOUND_BODY)
                return httpx.Response(
                    200, json={"executionId": "e", "status": "completed", "result": {"ok": True}}
                )
            return httpx.Response(
                200,
                json={"executionId": "e", "status": "started", "flowId": "f", "blockCount": 1},
            )

        monkeypatch.setattr(time, "sleep", lambda _s: None)

        client = make_sync_client(handler)
        job = client.flow("a/b/c").execute_async(message="hi")
        final = job.wait(timeout=10.0, poll_interval=0.1)
        client.close()
        assert final.status == "completed"
        assert call_count[0] == 2

    def test_sync_404_after_grace_propagates(self, monkeypatch):
        def handler(request):
            if "jobs/" in request.url.path and request.method == "GET":
                return httpx.Response(404, json=_NOT_FOUND_BODY)
            return httpx.Response(
                200,
                json={"executionId": "e", "status": "started", "flowId": "f", "blockCount": 1},
            )

        monkeypatch.setattr(time, "sleep", lambda _s: None)

        client = make_sync_client(handler)
        job = client.flow("a/b/c").execute_async(message="hi")
        job._submitted_at = time.monotonic() - 10.0
        with pytest.raises(FlowNotFoundError):
            job.wait(timeout=10.0, poll_interval=0.1)
        client.close()
