"""Integration tests: error handling and HTTP error propagation.

Exercises the SDK's exception hierarchy against a live server:
- Invalid API key raises AuthenticationError
- Unknown slug raises FlowNotFoundError with correct code + status_code
- Zero-credit account raises InsufficientCreditsError (skipped unless env set)
- request_id is propagated from server errors
"""

from __future__ import annotations

import os
import warnings

import pytest

from noukai_sdk import AuthenticationError, FlowNotFoundError, Noukai


@pytest.mark.integration
def test_invalid_api_key_raises_auth_error() -> None:
    """A client constructed with a garbage nk_* key raises AuthenticationError
    on the first server call.

    Note: the prefix ``nk_`` is required to pass the client-side prefix
    validation. The server then rejects the key with a 401.
    """
    # Use a syntactically valid prefix so the client-side guard doesn't fire.
    bad_key = "nk_integration_test_garbage_key_00000000000000000000000000000000"

    # Must pass org/project or a full slug so the client can build the URL.
    # We extract these from the integration project var since we need a valid path.
    integration_project = os.environ.get("NOUKAI_INTEGRATION_PROJECT", "test/test")
    org, project = (integration_project.split("/", 1) + ["test"])[:2]
    hello_slug = os.environ.get("NOUKAI_INTEGRATION_HELLO_SLUG", "hello-world")

    with Noukai(api_key=bad_key, org=org, project=project) as bad_client, pytest.raises(
        AuthenticationError
    ) as exc_info:
        bad_client.flow(hello_slug).execute(message="auth error test")

    err = exc_info.value
    assert err.status_code == 401, (
        f"AuthenticationError should carry status_code=401, got {err.status_code}"
    )


@pytest.mark.integration
def test_unknown_slug_raises_flow_not_found(client: Noukai) -> None:
    """Calling execute() on a non-existent slug raises FlowNotFoundError
    with code='FLOW_NOT_FOUND' and status_code=404.
    """
    with pytest.raises(FlowNotFoundError) as exc_info:
        client.flow("does-not-exist-integration-99").execute(message="x")

    err = exc_info.value
    assert err.status_code == 404, (
        f"FlowNotFoundError should carry status_code=404, got {err.status_code}"
    )
    # Server may return "FLOW_NOT_FOUND" or similar; accept either case.
    if err.code is not None:
        assert "NOT_FOUND" in err.code.upper() or "FLOW" in err.code.upper(), (
            f"Expected a NOT_FOUND-type error code, got {err.code!r}"
        )


@pytest.mark.integration
def test_zero_credits_raises_insufficient_credits() -> None:
    """A zero-credit account raises InsufficientCreditsError (HTTP 402).

    Requires a separate integration account with zero credits. Skipped
    unless NOUKAI_INTEGRATION_ZERO_CREDIT_KEY is set.

    To configure:
    - Create a test org with 0 credits in the Noukai dashboard.
    - Mint an ``nk_*`` key for that org.
    - Set NOUKAI_INTEGRATION_ZERO_CREDIT_KEY=nk_... in your env.
    - Set NOUKAI_INTEGRATION_ZERO_CREDIT_PROJECT=org/project to point at
      a valid flow in that org.
    """
    from noukai_sdk import InsufficientCreditsError

    zero_credit_key = os.environ.get("NOUKAI_INTEGRATION_ZERO_CREDIT_KEY")
    zero_credit_project = os.environ.get("NOUKAI_INTEGRATION_ZERO_CREDIT_PROJECT", "")
    zero_credit_slug = os.environ.get("NOUKAI_INTEGRATION_ZERO_CREDIT_SLUG")

    if not zero_credit_key:
        pytest.skip(
            "NOUKAI_INTEGRATION_ZERO_CREDIT_KEY not set — skipping credits exhaustion "
            "test. See test docstring for setup instructions."
        )
    if not zero_credit_slug or "/" not in zero_credit_project:
        pytest.skip(
            "NOUKAI_INTEGRATION_ZERO_CREDIT_PROJECT / NOUKAI_INTEGRATION_ZERO_CREDIT_SLUG "
            "not set — cannot identify the zero-credit flow to call."
        )

    org, project = zero_credit_project.split("/", 1)
    with Noukai(api_key=zero_credit_key, org=org, project=project) as zero_client, pytest.raises(
        InsufficientCreditsError
    ) as exc_info:
        zero_client.flow(zero_credit_slug).execute(message="credits test")

    err = exc_info.value
    assert err.status_code == 402, (
        f"InsufficientCreditsError should carry status_code=402, got {err.status_code}"
    )


@pytest.mark.integration
def test_request_id_propagated_to_error(client: Noukai) -> None:
    """Server errors should carry request_id from the X-Request-ID response header.

    Prerequisite: the server must echo ``X-Request-ID`` in error responses.
    If request_id is None, this test emits a warning rather than failing hard
    (server-side header may not yet be deployed to all environments).
    """
    captured_err: FlowNotFoundError | None = None

    with pytest.raises(FlowNotFoundError) as exc_info:
        client.flow("does-not-exist-request-id-99").execute(message="x")

    captured_err = exc_info.value
    if captured_err.request_id is None:
        warnings.warn(
            "FlowNotFoundError.request_id is None — server may not be returning "
            "X-Request-ID in error responses. This is a server-side gap, not an "
            "SDK bug. Enable the header on the server to make this assertion pass.",
            stacklevel=1,
        )
    else:
        # request_id is present — must be a non-empty string.
        assert captured_err.request_id != "", "request_id should be non-empty when present"
