"""Integration tests: server-side async (queue-backed) execution.

Exercises the execute_async() / Job / AsyncJob path:
- Immediate Job returned with execution_id
- poll() returns current JobStatus
- wait() drives polling until terminal state
- wait() raises APITimeoutError on zero-second timeout
- Async client parity
"""

from __future__ import annotations

import pytest

from noukai_sdk import APITimeoutError, AsyncFlow, AsyncJob, Flow, Job, JobStatus


@pytest.mark.integration
def test_execute_async_returns_job(hello_flow: Flow) -> None:
    """execute_async() returns a Job with an execution_id immediately."""
    job = hello_flow.execute_async(message="queue test")

    assert isinstance(job, Job)
    assert job.execution_id is not None
    assert job.execution_id != ""
    assert job.flow_id is not None


@pytest.mark.integration
def test_job_poll_returns_status(hello_flow: Flow) -> None:
    """job.poll() returns a JobStatus with a recognised status value."""
    job = hello_flow.execute_async(message="poll test")
    status = job.poll()

    assert isinstance(status, JobStatus)
    assert status.status in ("pending", "running", "completed", "failed")
    assert status.execution_id == job.execution_id


@pytest.mark.integration
def test_job_wait_completes(hello_flow: Flow) -> None:
    """job.wait() blocks until the job reaches a terminal state."""
    job = hello_flow.execute_async(message="wait test")
    final = job.wait(timeout=60, poll_interval=1)

    assert isinstance(final, JobStatus)
    assert final.status in ("completed", "failed")


@pytest.mark.integration
def test_job_wait_timeout(hello_flow: Flow) -> None:
    """job.wait(timeout=0.01) raises APITimeoutError before completing."""
    job = hello_flow.execute_async(message="timeout test")

    with pytest.raises(APITimeoutError):
        # 10 ms is short enough that no real server can respond in time.
        job.wait(timeout=0.01, poll_interval=0.001)


@pytest.mark.integration
async def test_async_job_wait(async_hello_flow: AsyncFlow) -> None:
    """AsyncJob.wait() awaits until the job completes or times out."""
    job = await async_hello_flow.execute_async(message="async queue test")

    assert isinstance(job, AsyncJob)
    assert job.execution_id is not None

    final = await job.wait(timeout=60, poll_interval=1)

    assert isinstance(final, JobStatus)
    assert final.status in ("completed", "failed")
