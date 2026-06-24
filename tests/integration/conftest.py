"""Integration tests against a real Noukai server.

Skipped by default. Set these env vars (or `cp .env.example .env`) to run:
- NOUKAI_INTEGRATION_KEY: a real ``nk_*`` API key
- NOUKAI_INTEGRATION_PROJECT: "org/project" pair (e.g. "abc/integration-tests")
- NOUKAI_INTEGRATION_HELLO_SLUG: slug of the hello-world fixture flow
- NOUKAI_INTEGRATION_TWO_STEP_SLUG: slug of the two-step fixture flow
- NOUKAI_INTEGRATION_TOOLS_SLUG: slug of the tools-enabled fixture flow

For local dev against ``localhost:8080``, also set ``NOUKAI_ENV=dev``.

Run: ``uv run pytest tests/integration/ -v``
Skip: just run ``pytest`` without env vars — entire suite skips.

The ``.env`` file at the SDK root is auto-loaded by python-dotenv before any
env-var reads below. This only affects test runs; the SDK itself never reads
``.env``.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

# Auto-load .env from the SDK root (development/noukai/sdk/python/.env).
# Tolerant if python-dotenv isn't installed or .env doesn't exist.
try:
    from dotenv import load_dotenv

    _env_file = Path(__file__).resolve().parents[2] / ".env"
    if _env_file.is_file():
        load_dotenv(_env_file, override=False)
except ImportError:
    pass

from noukai_sdk import AsyncFlow, AsyncNoukai, Flow, Noukai

INTEGRATION_KEY = os.environ.get("NOUKAI_INTEGRATION_KEY")
INTEGRATION_PROJECT = os.environ.get("NOUKAI_INTEGRATION_PROJECT", "")
HELLO_SLUG = os.environ.get("NOUKAI_INTEGRATION_HELLO_SLUG")
TWO_STEP_SLUG = os.environ.get("NOUKAI_INTEGRATION_TWO_STEP_SLUG")
TOOLS_SLUG = os.environ.get("NOUKAI_INTEGRATION_TOOLS_SLUG")


def _required_envs_set() -> bool:
    return all(
        [
            INTEGRATION_KEY,
            "/" in INTEGRATION_PROJECT,
            HELLO_SLUG,
        ]
    )


# Apply to the whole package — skips entire integration suite if envs missing.
pytestmark = pytest.mark.skipif(
    not _required_envs_set(),
    reason="Integration env vars not set; see tests/integration/README.md",
)


def _split_project() -> tuple[str, str]:
    org, project = INTEGRATION_PROJECT.split("/", 1)
    return org, project


@pytest.fixture
def client() -> Iterator[Noukai]:
    org, project = _split_project()
    with Noukai(api_key=INTEGRATION_KEY, org=org, project=project) as c:
        yield c


@pytest.fixture
async def async_client() -> AsyncIterator[AsyncNoukai]:
    org, project = _split_project()
    async with AsyncNoukai(api_key=INTEGRATION_KEY, org=org, project=project) as c:
        yield c


@pytest.fixture
def hello_flow(client: Noukai) -> Flow:
    """A flow that takes ``message`` and echoes a simple response."""
    return client.flow(HELLO_SLUG)  # type: ignore[arg-type]


@pytest.fixture
def two_step_flow(client: Noukai) -> Flow:
    """A two-block flow used to test steps()/events() iteration."""
    if not TWO_STEP_SLUG:
        pytest.skip("NOUKAI_INTEGRATION_TWO_STEP_SLUG not set")
    return client.flow(TWO_STEP_SLUG)


@pytest.fixture
def tools_flow(client: Noukai) -> Flow:
    """A flow configured with ``tools`` for tool-call resume testing."""
    if not TOOLS_SLUG:
        pytest.skip("NOUKAI_INTEGRATION_TOOLS_SLUG not set")
    return client.flow(TOOLS_SLUG)


@pytest.fixture
async def async_hello_flow(async_client: AsyncNoukai) -> AsyncFlow:
    """Async version of hello_flow."""
    return async_client.flow(HELLO_SLUG)  # type: ignore[arg-type]


@pytest.fixture
async def async_tools_flow(async_client: AsyncNoukai) -> AsyncFlow:
    """Async version of tools_flow."""
    if not TOOLS_SLUG:
        pytest.skip("NOUKAI_INTEGRATION_TOOLS_SLUG not set")
    return async_client.flow(TOOLS_SLUG)


@pytest.fixture
async def async_two_step_flow(async_client: AsyncNoukai) -> AsyncFlow:
    """Async version of two_step_flow."""
    if not TWO_STEP_SLUG:
        pytest.skip("NOUKAI_INTEGRATION_TWO_STEP_SLUG not set")
    return async_client.flow(TWO_STEP_SLUG)
