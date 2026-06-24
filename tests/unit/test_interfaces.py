"""Smoke tests for public interface shape. These are NOT behaviour tests;
they verify that the public API is importable and that signatures haven't
drifted (catches accidental param removal in later refactors)."""

import inspect

import pytest

import noukai_sdk
from noukai_sdk import (
    AsyncNoukai,
    Flow,
    Noukai,
    Run,
)


class TestPublicSurface:
    def test_top_level_exports_present(self) -> None:
        for name in noukai_sdk.__all__:
            assert hasattr(noukai_sdk, name), f"Missing public export: {name}"

    def test_no_private_re_exports(self) -> None:
        """Private names (single leading underscore) MUST NOT be in __all__.

        Dunder names (``__foo__``) are allowed — ``__version__`` is the
        canonical example of a public dunder in a package's ``__all__``.
        """
        privates = [
            n
            for n in noukai_sdk.__all__
            if n.startswith("_") and not (n.startswith("__") and n.endswith("__"))
        ]
        assert privates == [], f"Private re-exports in __all__: {privates}"

    def test_version_present(self) -> None:
        from noukai_sdk._version import __version__

        assert isinstance(noukai_sdk.__version__, str)
        assert noukai_sdk.__version__ == __version__


class TestNoukaiClient:
    def test_constructor_signature(self) -> None:
        sig = inspect.signature(Noukai.__init__)
        params = set(sig.parameters.keys())
        assert {
            "api_key",
            "env",
            "timeout",
            "max_retries",
            "log_handler",
            "log_payloads",
        }.issubset(params)

    def test_supports_context_manager_protocol(self) -> None:
        assert hasattr(Noukai, "__enter__")
        assert hasattr(Noukai, "__exit__")
        assert hasattr(Noukai, "close")

    def test_async_supports_async_context_manager(self) -> None:
        assert hasattr(AsyncNoukai, "__aenter__")
        assert hasattr(AsyncNoukai, "__aexit__")
        assert hasattr(AsyncNoukai, "aclose")


class TestFlowInterface:
    def test_execute_signature(self) -> None:
        sig = inspect.signature(Flow.execute)
        params = set(sig.parameters.keys())
        assert {
            "message",
            "parameters",
            "block_overrides",
            "tools",
            "tool_handler",
            "max_tool_rounds",
            "trace",
            "version",
            "timeout",
        }.issubset(params)

    def test_steps_returns_iterator_annotation(self) -> None:
        sig = inspect.signature(Flow.steps)
        # Return type uses string form; we just verify it doesn't take
        # `run_remaining` (that's an `events()` param).
        assert "run_remaining" not in sig.parameters

    def test_events_takes_run_remaining(self) -> None:
        sig = inspect.signature(Flow.events)
        assert "run_remaining" in sig.parameters
        assert sig.parameters["run_remaining"].default is False

    def test_run_returns_run_proxy(self) -> None:
        # Just verify the method exists and takes one positional arg.
        # Skip ``self`` (present when inspecting via the class, not an instance).
        sig = inspect.signature(Flow.run)
        params = [p for p in sig.parameters.values() if p.name != "self"]
        assert params[0].name == "execution_id"


class TestRunInterface:
    def test_step_trace_attempt_param(self) -> None:
        sig = inspect.signature(Run.step_trace)
        assert sig.parameters["attempt"].default == "latest"


class TestNotYetImplemented:
    """Every interface method should raise the expected exception at
    construction time. After Phase 4, Noukai() raises AuthenticationError
    (not NotImplementedError) when no API key is available."""

    def test_noukai_init_raises_auth_error_without_key(self, monkeypatch) -> None:
        monkeypatch.delenv("NOUKAI_API_KEY", raising=False)
        from noukai_sdk import AuthenticationError

        with pytest.raises(AuthenticationError):
            Noukai()
