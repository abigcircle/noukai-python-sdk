"""Phase 8: Parity gate — sync and async public API names must stay aligned.

Ensures that as the SDK evolves, additions to ``AsyncFlow`` / ``AsyncNoukai``
/ ``AsyncRun`` are mirrored in their sync counterparts (or vice versa).
"""

import inspect

import pytest

from noukai_sdk import AsyncFlow, AsyncNoukai, AsyncRun, Flow, Noukai, Run


def public_methods(cls: type) -> set[str]:
    """Return all non-dunder callable names on *cls* (including inherited)."""
    return {
        name
        for name, member in inspect.getmembers(cls, predicate=callable)
        if not name.startswith("_")
    }


@pytest.mark.parametrize(
    ("async_cls", "sync_cls"),
    [
        (AsyncNoukai, Noukai),
        (AsyncFlow, Flow),
        (AsyncRun, Run),
    ],
)
def test_public_method_names_match(async_cls: type, sync_cls: type) -> None:
    """Sync and async classes expose the same public method names.

    Normalisation: ``aclose`` on the async client corresponds to ``close``
    on the sync client — these are excluded from the comparison so that the
    naming convention difference does not cause a false failure.
    """
    async_methods = public_methods(async_cls) - {"aclose"}
    sync_methods = public_methods(sync_cls) - {"close"}
    assert async_methods == sync_methods, (
        f"{async_cls.__name__} vs {sync_cls.__name__} differ.\n"
        f"  Only in async: {async_methods - sync_methods}\n"
        f"  Only in sync:  {sync_methods - async_methods}"
    )
