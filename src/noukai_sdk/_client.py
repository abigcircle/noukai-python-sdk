"""Top-level client classes.

``AsyncNoukai`` and ``Noukai`` are the public entry points. Both share the
same slug-parsing and env-var resolution logic; they differ in transport
(``AsyncTransport`` vs ``SyncTransport``) and method signatures.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from types import TracebackType
from typing import Any, Literal

from ._constants import (
    API_KEY_ENV_VAR,
    API_KEY_PREFIX,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    DEV_BASE_URL,
    ENV_ENV_VAR,
)
from ._errors import AuthenticationError
from ._flow import AsyncFlow, Flow
from ._transport import AsyncTransport, SyncTransport

NoukaiEnv = Literal["dev", "production"]


def _parse_flow_args(
    slug: str | None,
    org: str | None,
    project: str | None,
    *,
    default_org: str | None = None,
    default_project: str | None = None,
) -> tuple[str, str, str]:
    """Parse client.flow(...) calling forms into (org, project, slug).

    Three forms:
    - ``flow("slug")``                       — single segment, uses client defaults
    - ``flow("org/project/slug")``           — fully qualified, overrides defaults
    - ``flow(org=..., project=..., slug=...)`` — explicit kwargs, overrides defaults

    Disambiguation:
    - If ``org`` or ``project`` is provided as a kwarg, we're in kwargs form.
    - If ``slug`` contains ``/``, we're in string form (must be 3 parts).
      A multi-part string + kwargs is a conflict (``ValueError``).
    - Single-segment string falls back to client-level defaults; absent
      defaults, raises a helpful ``ValueError``.
    """
    has_org_or_project = (org is not None) or (project is not None)
    has_slug = slug is not None
    slug_has_slashes = has_slug and "/" in slug  # type: ignore[operator]

    # Conflict: caller passed a multi-part string AND org/project kwargs.
    if slug_has_slashes and has_org_or_project:
        raise ValueError("Pass either a slug string OR org/project/slug kwargs, not both.")

    # String form
    if has_slug and not has_org_or_project:
        parts = slug.split("/")  # type: ignore[union-attr]

        # Single-segment: use client defaults.
        if len(parts) == 1:
            if not parts[0]:
                raise ValueError("Slug string is empty.")
            if not default_org or not default_project:
                raise ValueError(
                    f"flow({slug!r}) requires the client to be constructed with "
                    f"org and project defaults: Noukai(org=..., project=...). "
                    f"Otherwise pass a fully-qualified 'org/project/slug' string."
                )
            return default_org, default_project, parts[0]

        # Three-segment: fully qualified, overrides defaults.
        if len(parts) == 3 and all(parts):
            return parts[0], parts[1], parts[2]

        raise ValueError(
            f"Slug string must be either a single slug name (with client "
            f"org/project defaults) or 'org/project/slug'; got: {slug!r}"
        )

    # Kwargs form: org=, project=, slug= all provided
    if has_org_or_project or has_slug:
        if not all((org, project, slug)):
            raise ValueError("Kwargs form requires all of org=, project=, slug=.")
        return org, project, slug  # type: ignore[return-value]

    raise ValueError("Must provide either a slug string or org/project/slug kwargs.")


def _validate_org_project(org: str | None, project: str | None) -> None:
    """Reject half-defaults — both or neither."""
    if (org is None) != (project is None):
        raise ValueError("Noukai: `org` and `project` must be provided together, or both omitted.")


def _resolve_base_url(env: NoukaiEnv | None) -> str:
    """Resolve the base URL from the deployment env shortcut:

    1. ``env="dev"`` argument OR ``NOUKAI_ENV=dev`` env var → DEV_BASE_URL
    2. Production default (``https://api.noukai.xyz/api/v1``)
    """
    env_mode = env or os.environ.get(ENV_ENV_VAR)
    if env_mode in ("dev", "development"):
        return DEV_BASE_URL
    return DEFAULT_BASE_URL


def _resolve_credentials(
    api_key: str | None,
    env: NoukaiEnv | None = None,
) -> tuple[str, str]:
    """Resolve API key and base URL from args + env vars, validate key prefix."""
    resolved_key = api_key or os.environ.get(API_KEY_ENV_VAR)
    if not resolved_key:
        raise AuthenticationError(f"No API key provided. Pass api_key= or set {API_KEY_ENV_VAR}.")
    if not resolved_key.startswith(API_KEY_PREFIX):
        raise AuthenticationError(f"Invalid API key prefix. Expected '{API_KEY_PREFIX}'.")
    resolved_url = _resolve_base_url(env)
    return resolved_key, resolved_url


def _build_sync_transport(
    api_key: str | None,
    env: NoukaiEnv | None,
    timeout: float | None,
    max_retries: int | None,
    log_handler: Callable[[dict[str, Any]], None] | None,
    log_payloads: bool,
    session_id: str | None = None,
) -> SyncTransport:
    """Resolve env vars, validate key, and construct SyncTransport."""
    resolved_key, resolved_url = _resolve_credentials(api_key, env)
    return SyncTransport(
        api_key=resolved_key,
        base_url=resolved_url,
        timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
        max_retries=max_retries or DEFAULT_MAX_RETRIES,
        log_handler=log_handler,
        log_payloads=log_payloads,
        default_session_id=session_id,
    )


def _build_async_transport(
    api_key: str | None,
    env: NoukaiEnv | None,
    timeout: float | None,
    max_retries: int | None,
    log_handler: Callable[[dict[str, Any]], None] | None,
    log_payloads: bool,
    session_id: str | None = None,
) -> AsyncTransport:
    """Resolve env vars, validate key, and construct AsyncTransport."""
    resolved_key, resolved_url = _resolve_credentials(api_key, env)
    return AsyncTransport(
        api_key=resolved_key,
        base_url=resolved_url,
        timeout=timeout or DEFAULT_TIMEOUT_SECONDS,
        max_retries=max_retries or DEFAULT_MAX_RETRIES,
        log_handler=log_handler,
        log_payloads=log_payloads,
        default_session_id=session_id,
    )


class Noukai:
    """Synchronous Noukai client.

    Holds an HTTP connection pool and credentials. Construct once per
    process; use as a context manager OR call `.close()` to release the
    pool when done.

    Args:
        api_key: Noukai API key (starts with ``nk_``). If omitted, reads
            ``NOUKAI_API_KEY`` env var. Raises ``AuthenticationError`` at
            construction if no key is found or prefix is wrong.
        env: Deployment shortcut: ``"dev"`` points at
            ``http://localhost:8080/api/v1``; ``"production"`` (default)
            points at ``https://api.noukai.xyz/api/v1``. Falls back to
            ``NOUKAI_ENV`` env var. The SDK does not accept an arbitrary
            base URL — all requests target Noukai's hosted endpoints.
        org: Default organisation. When set with ``project``,
            ``client.flow("slug")`` uses the single slug; a fully-qualified
            ``"org/project/slug"`` string or kwargs still override.
        project: Default project. Required when ``org`` is set.
        session_id: Client-level default session id. Lowest-priority in the
            precedence chain: kwarg > client default > trace_scope contextvar
            > None. Propagated to all ``Flow`` proxies returned by
            ``flow(...)``. Wired to header injection in Phase 5 (capture) and
            Phase 6 (replay).
        timeout: Default request timeout in seconds (default 300).
        max_retries: Default retry count on 5xx (default 1, exponential backoff).
        log_handler: Optional callable receiving structured request/response
            events. Payloads omitted unless ``log_payloads=True``.
        log_payloads: When True, request/response bodies are passed to
            ``log_handler``. Default False — protects credentials and PII.

    Example:
        >>> with Noukai(org="acme", project="spelling") as client:
        ...     result = client.flow("grade-3").execute(message="hello")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        env: NoukaiEnv | None = None,
        org: str | None = None,
        project: str | None = None,
        session_id: str | None = None,  # NEW — see design 20260605-SDK-replay-decorator
        timeout: float | None = None,
        max_retries: int | None = None,
        log_handler: Callable[[dict[str, Any]], None] | None = None,
        log_payloads: bool = False,
    ) -> None:
        _validate_org_project(org, project)
        self.default_org = org
        self.default_project = project
        self._default_session_id = session_id
        self._transport = _build_sync_transport(
            api_key, env, timeout, max_retries, log_handler, log_payloads, session_id
        )

    def flow(
        self,
        slug: str | None = None,
        *,
        org: str | None = None,
        project: str | None = None,
    ) -> Flow:
        """Bind a Flow object for a given slug.

        Three forms supported:

            client.flow("slug")                                # uses client defaults
            client.flow("acme/spelling/grade-3")               # fully qualified
            client.flow(org="acme", project="spelling", slug="grade-3")

        Args:
            slug: Either a single-segment slug (uses client ``org``/``project``
                defaults), a fully-qualified ``"org/project/slug"`` string,
                or - when used with kwargs - the slug name only.
            org: Organisation identifier. Overrides the client default.
            project: Project identifier. Overrides the client default.

        Returns:
            A ``Flow`` proxy bound to the given slug.

        Raises:
            ValueError: when the slug string is malformed, kwargs are
                incomplete, or a single-segment slug is given without
                client-level ``org``/``project`` defaults.
        """
        _org, _project, _slug = _parse_flow_args(
            slug,
            org,
            project,
            default_org=self.default_org,
            default_project=self.default_project,
        )
        return Flow(self._transport, _org, _project, _slug)

    def close(self) -> None:
        """Release the underlying HTTP connection pool. Safe to call repeatedly."""
        self._transport.close()

    def __enter__(self) -> Noukai:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()


class AsyncNoukai:
    """Asynchronous Noukai client.

    Holds an HTTP connection pool and credentials. Construct once per
    process; use as an async context manager OR call ``await .aclose()``
    to release the pool when done.

    Args:
        api_key: Noukai API key (starts with ``nk_``). If omitted, reads
            ``NOUKAI_API_KEY`` env var. Raises ``AuthenticationError`` at
            construction if no key is found or prefix is wrong.
        env: Deployment shortcut: ``"dev"`` points at
            ``http://localhost:8080/api/v1``; ``"production"`` (default)
            points at ``https://api.noukai.xyz/api/v1``. Falls back to
            ``NOUKAI_ENV`` env var. The SDK does not accept an arbitrary
            base URL — all requests target Noukai's hosted endpoints.
        org: Default organisation. When set with ``project``,
            ``client.flow("slug")`` uses the single slug; a fully-qualified
            ``"org/project/slug"`` string or kwargs still override.
        project: Default project. Required when ``org`` is set.
        session_id: Client-level default session id. Lowest-priority in the
            precedence chain: kwarg > client default > trace_scope contextvar
            > None. Propagated to all ``AsyncFlow`` proxies returned by
            ``flow(...)``. Wired to header injection in Phase 5 (capture) and
            Phase 6 (replay).
        timeout: Default request timeout in seconds (default 300).
        max_retries: Default retry count on 5xx (default 1, exponential backoff).
        log_handler: Optional callable receiving structured request/response
            events. Payloads omitted unless ``log_payloads=True``.
        log_payloads: When True, request/response bodies are passed to
            ``log_handler``. Default False — protects credentials and PII.

    Example:
        >>> async with AsyncNoukai(org="acme", project="spelling") as client:
        ...     result = await client.flow("grade-3").execute(message="hello")
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        env: NoukaiEnv | None = None,
        org: str | None = None,
        project: str | None = None,
        session_id: str | None = None,  # NEW — see design 20260605-SDK-replay-decorator
        timeout: float | None = None,
        max_retries: int | None = None,
        log_handler: Callable[[dict[str, Any]], None] | None = None,
        log_payloads: bool = False,
    ) -> None:
        _validate_org_project(org, project)
        self.default_org = org
        self.default_project = project
        self._default_session_id = session_id
        self._transport = _build_async_transport(
            api_key, env, timeout, max_retries, log_handler, log_payloads, session_id
        )

    def flow(
        self,
        slug: str | None = None,
        *,
        org: str | None = None,
        project: str | None = None,
    ) -> AsyncFlow:
        """Bind an AsyncFlow object for a given slug.

        Three forms supported:

            client.flow("slug")                                # uses client defaults
            client.flow("acme/spelling/grade-3")               # fully qualified
            client.flow(org="acme", project="spelling", slug="grade-3")

        Args:
            slug: Either a single-segment slug (uses client ``org``/``project``
                defaults), a fully-qualified ``"org/project/slug"`` string,
                or - when used with kwargs - the slug name only.
            org: Organisation identifier. Overrides the client default.
            project: Project identifier. Overrides the client default.

        Returns:
            An ``AsyncFlow`` proxy bound to the given slug.

        Raises:
            ValueError: when the slug string is malformed, kwargs are
                incomplete, or a single-segment slug is given without
                client-level ``org``/``project`` defaults.
        """
        _org, _project, _slug = _parse_flow_args(
            slug,
            org,
            project,
            default_org=self.default_org,
            default_project=self.default_project,
        )
        return AsyncFlow(self._transport, _org, _project, _slug)

    async def aclose(self) -> None:
        """Release the underlying HTTP connection pool."""
        await self._transport.aclose()

    async def __aenter__(self) -> AsyncNoukai:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()
