"""Centralized backend URL paths for the Noukai SDK.

SINGLE SOURCE OF TRUTH for every wire path the SDK calls. This file exists
so an auditor can read one place to verify the SDK matches the backend
route registrations. If a route changes server-side, change this file —
every call site updates automatically.

Backend handler references (``router-ai-slugs``):

- ``/seq/{org}/{project}/{slug}/execute``     -- seqflow_routes.py POST
- ``/seq/{org}/{project}/{slug}/step``        -- seqflow_routes.py POST
- ``/seq/{org}/{project}/{slug}/jobs``        -- seqflow_routes.py POST (queue)
- ``/seq/{org}/{project}/{slug}/jobs/{id}``   -- jobs_routes.py     GET
- ``/seq/{org}/{project}/{slug}/runs/{id}``   -- runs_routes.py     GET / sub
- ``/seq/sessions/{session_id}``              -- sessions_routes.py GET

Audit note: the BE routers all mount under ``APIRouter(prefix="/seq")``;
paths here include that prefix. If a router renames its prefix, update
``_SEQ_PREFIX`` below and all consumers will pick it up.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import quote

# Common backend prefix for all seqflow routes.
_SEQ_PREFIX: Final = "/seq"

# Session IDs are server-generated UUIDs. Accepting any other shape would let
# attacker-controlled values (e.g. from the X-Noukai-Replay header read by
# trace middleware) inject `/`, `..`, or `?` into the URL path and pivot the
# authenticated GET to a different endpoint under the same API base.
_UUID_RE: Final = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# Version selector: "draft" or a positive int (= /v{N}).
VersionSegment = str | int


# ---------------------------------------------------------------------------
# Versioned flow base (org/project/slug, optionally pinned to /v{N})
# ---------------------------------------------------------------------------


def flow_base(
    org: str,
    project: str,
    slug: str,
    version: VersionSegment = "draft",
) -> str:
    """Build the versioned base path for a flow.

    - ``"draft"`` → ``/seq/{org}/{project}/{slug}``
    - ``<int>``   → ``/seq/{org}/{project}/{slug}/v{N}``

    ``"production"`` is intentionally unsupported here — callers must reject
    it before reaching this helper (server-side body-field routing not
    deployed).
    """
    base = f"{_SEQ_PREFIX}/{org}/{project}/{slug}"
    if isinstance(version, int):
        return f"{base}/v{version}"
    return base


# ---------------------------------------------------------------------------
# Flow execution endpoints
# ---------------------------------------------------------------------------


def flow_execute_path(
    org: str,
    project: str,
    slug: str,
    version: VersionSegment = "draft",
) -> str:
    """``POST /seq/{org}/{project}/{slug}[/vN]/execute`` -- synchronous execute."""
    return f"{flow_base(org, project, slug, version)}/execute"


def flow_step_path(
    org: str,
    project: str,
    slug: str,
    version: VersionSegment = "draft",
) -> str:
    """``POST /seq/{org}/{project}/{slug}[/vN]/step`` -- SSE step stream."""
    return f"{flow_base(org, project, slug, version)}/step"


def flow_jobs_submit_path(
    org: str,
    project: str,
    slug: str,
    version: VersionSegment = "draft",
) -> str:
    """``POST /seq/{org}/{project}/{slug}[/vN]/jobs`` -- queue-backed submit."""
    return f"{flow_base(org, project, slug, version)}/jobs"


def flow_job_poll_path(
    org: str,
    project: str,
    slug: str,
    execution_id: str,
) -> str:
    """``GET /seq/{org}/{project}/{slug}/jobs/{execution_id}`` -- poll job."""
    return f"{flow_base(org, project, slug)}/jobs/{execution_id}"


# ---------------------------------------------------------------------------
# Run trace endpoints
# ---------------------------------------------------------------------------


def run_path(
    org: str,
    project: str,
    slug: str,
    execution_id: str,
) -> str:
    """``GET /seq/{org}/{project}/{slug}/runs/{execution_id}`` -- run summary."""
    return f"{flow_base(org, project, slug)}/runs/{execution_id}"


# ---------------------------------------------------------------------------
# Session replay endpoint
# ---------------------------------------------------------------------------


def session_path(session_id: str) -> str:
    """``GET /seq/sessions/{session_id}`` -- replay cassette fetch.

    Backend: ``router-ai-slugs/api/sessions_routes.py``.
    Design: ``20260605-BE-execution-session-grouping``.

    The route is NOT scoped by org/project -- auth is per-flow-run inside
    the handler.

    ``session_id`` is validated as a UUID before interpolation. Trace
    middleware reads it from an attacker-controllable HTTP header; rejecting
    non-UUID shapes here prevents path traversal into other authenticated
    endpoints. The segment is also URL-encoded as a backstop.
    """
    from ._errors import ReplayInvalidSessionError

    if not _UUID_RE.match(session_id):
        raise ReplayInvalidSessionError(
            f"Invalid session id format (expected UUID): {session_id!r}",
            status_code=400,
        )
    return f"{_SEQ_PREFIX}/sessions/{quote(session_id, safe='')}"
