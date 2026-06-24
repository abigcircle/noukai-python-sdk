"""Job handle for async (queue-backed) executions."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

from ._constants import DEFAULT_JOB_POLL_INTERVAL_SECONDS, DEFAULT_JOB_WAIT_TIMEOUT_SECONDS
from ._errors import APITimeoutError, FlowNotFoundError
from ._models.responses import JobStatus
from ._paths import flow_job_poll_path

# Grace window during which a 404 from the status endpoint is treated as
# "job not yet registered" rather than a missing execution. Covers the brief
# race between ``execute_async`` returning and the orchestrator-worker
# inserting the flow_runs row.
_SUBMISSION_GRACE_SECONDS = 5.0

if TYPE_CHECKING:
    from ._transport import AsyncTransport, SyncTransport


class Job:
    """Handle for an async flow execution. Returned by ``Flow.execute_async``.

    Use ``.poll()`` for a one-shot status check, or ``.wait()`` to block
    until the execution reaches a terminal state.
    """

    def __init__(
        self,
        transport: SyncTransport,
        org: str,
        project: str,
        slug: str,
        execution_id: str,
        flow_id: str,
        *,
        submitted_at: float | None = None,
    ) -> None:
        self._transport = transport
        self._org = org
        self._project = project
        self._slug = slug
        self._execution_id = execution_id
        self._flow_id = flow_id
        # ``submitted_at`` is a test seam — production callers should leave it
        # unset and let it default to ``time.monotonic()`` at construction time.
        self._submitted_at = time.monotonic() if submitted_at is None else submitted_at

    @property
    def execution_id(self) -> str:
        """The execution ID for this async job."""
        return self._execution_id

    @property
    def flow_id(self) -> str:
        """The flow ID that this job was submitted against."""
        return self._flow_id

    def poll(self, *, timeout: float | None = None) -> JobStatus:
        """One-shot status check. Does not block beyond the HTTP call.

        Race-window behavior: between the moment ``execute_async`` returns and
        the moment the orchestrator-worker inserts the ``flow_runs`` row, the
        status endpoint returns 404. Within the 5s grace window after
        submission, we synthesize a ``pending`` status rather than surface a
        misleading ``FlowNotFoundError``. After the grace window, real 404s
        propagate.

        Args:
            timeout: Per-request timeout override (seconds).

        Returns:
            The current ``JobStatus`` for this execution.

        Raises:
            FlowNotFoundError: execution_id not found after the grace window.
            APIConnectionError: network failure.
            APITimeoutError: request timed out.
        """
        url = flow_job_poll_path(self._org, self._project, self._slug, self._execution_id)
        try:
            resp = self._transport.request("GET", url, timeout=timeout)
        except FlowNotFoundError:
            if time.monotonic() - self._submitted_at < _SUBMISSION_GRACE_SECONDS:
                return JobStatus.model_validate(
                    {"executionId": self._execution_id, "status": "pending"}
                )
            raise
        return JobStatus.model_validate(resp.body)

    def wait(
        self,
        *,
        timeout: float = DEFAULT_JOB_WAIT_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_JOB_POLL_INTERVAL_SECONDS,
    ) -> JobStatus:
        """Poll until status is terminal (``'completed'`` or ``'failed'``) or
        ``timeout`` elapses.

        Calls ``poll()`` repeatedly with ``poll_interval`` seconds between
        attempts. Uses ``time.sleep`` for the delay (do not call from inside a
        running event loop — use ``AsyncJob.wait`` instead).

        Args:
            timeout: Maximum time to wait in seconds (default 300).
            poll_interval: Seconds between poll attempts (default 2.0).

        Returns:
            The terminal ``JobStatus`` (status ``'completed'`` or ``'failed'``).

        Raises:
            APITimeoutError: when ``timeout`` elapses without a terminal status.
            APIConnectionError: network failure during polling.
        """
        deadline = time.monotonic() + timeout
        while True:
            status = self.poll()
            if status.status in ("completed", "failed"):
                return status
            if time.monotonic() >= deadline:
                raise APITimeoutError(
                    f"Job {self._execution_id} did not complete within {timeout}s"
                )
            time.sleep(poll_interval)


class AsyncJob:
    """Async handle for an async flow execution.

    Returned by ``AsyncFlow.execute_async``. Use ``await .poll()`` for a
    one-shot status check, or ``await .wait()`` to wait for completion.
    """

    def __init__(
        self,
        transport: AsyncTransport,
        org: str,
        project: str,
        slug: str,
        execution_id: str,
        flow_id: str,
        *,
        submitted_at: float | None = None,
    ) -> None:
        self._transport = transport
        self._org = org
        self._project = project
        self._slug = slug
        self._execution_id = execution_id
        self._flow_id = flow_id
        # ``submitted_at`` is a test seam — production callers should leave it
        # unset and let it default to ``time.monotonic()`` at construction time.
        self._submitted_at = time.monotonic() if submitted_at is None else submitted_at

    @property
    def execution_id(self) -> str:
        """The execution ID for this async job."""
        return self._execution_id

    @property
    def flow_id(self) -> str:
        """The flow ID that this job was submitted against."""
        return self._flow_id

    async def poll(self, *, timeout: float | None = None) -> JobStatus:
        """One-shot async status check. Does not block beyond the HTTP call.

        Race-window behavior: between the moment ``execute_async`` returns and
        the moment the orchestrator-worker inserts the ``flow_runs`` row, the
        status endpoint returns 404. Within the 5s grace window after
        submission, we synthesize a ``pending`` status rather than surface a
        misleading ``FlowNotFoundError``. After the grace window, real 404s
        propagate.

        Args:
            timeout: Per-request timeout override (seconds).

        Returns:
            The current ``JobStatus`` for this execution.

        Raises:
            FlowNotFoundError: execution_id not found after the grace window.
            APIConnectionError: network failure.
            APITimeoutError: request timed out.
        """
        url = flow_job_poll_path(self._org, self._project, self._slug, self._execution_id)
        try:
            resp = await self._transport.request("GET", url, timeout=timeout)
        except FlowNotFoundError:
            if time.monotonic() - self._submitted_at < _SUBMISSION_GRACE_SECONDS:
                return JobStatus.model_validate(
                    {"executionId": self._execution_id, "status": "pending"}
                )
            raise
        return JobStatus.model_validate(resp.body)

    async def wait(
        self,
        *,
        timeout: float = DEFAULT_JOB_WAIT_TIMEOUT_SECONDS,
        poll_interval: float = DEFAULT_JOB_POLL_INTERVAL_SECONDS,
    ) -> JobStatus:
        """Async poll until status is terminal (``'completed'`` or ``'failed'``)
        or ``timeout`` elapses.

        Uses ``asyncio.sleep`` between poll attempts.

        Args:
            timeout: Maximum time to wait in seconds (default 300).
            poll_interval: Seconds between poll attempts (default 2.0).

        Returns:
            The terminal ``JobStatus`` (status ``'completed'`` or ``'failed'``).

        Raises:
            APITimeoutError: when ``timeout`` elapses without a terminal status.
            APIConnectionError: network failure during polling.
        """
        deadline = time.monotonic() + timeout
        while True:
            status = await self.poll()
            if status.status in ("completed", "failed"):
                return status
            if time.monotonic() >= deadline:
                raise APITimeoutError(
                    f"Job {self._execution_id} did not complete within {timeout}s"
                )
            await asyncio.sleep(poll_interval)
