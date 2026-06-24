"""Noukai Python SDK.

Public surface re-exports. Anything importable as `noukai_sdk.X` is public
API; anything in `noukai_sdk._foo` is private and may change without notice.
"""

# Clients
from ._client import AsyncNoukai, Noukai

# Exceptions
# Replay / trace (design 20260605-SDK-replay-decorator)
from ._errors import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    FlowExecutionError,
    FlowNotFoundError,
    InsufficientCreditsError,
    NoukaiError,
    PermissionDeniedError,
    RateLimitError,
    ReplayDisabledError,
    ReplayError,
    ReplayForbiddenError,
    ReplayInvalidSessionError,
    ReplayLeftoverError,
    ReplayMissError,
    ReplayNoSnapshotsError,
    ReplaySessionExpiredError,
    ReplaySessionNotFoundError,
    ToolCallLimitError,
)

# Proxies (returned by client.flow() / flow.run() — surfaced for type hints)
from ._flow import AsyncFlow, Flow
from ._jobs import AsyncJob, Job

# Stream events
from ._models.events import (
    FlowCompleted,
    RunStarted,
    StepCompleted,
    StepFailed,
    StepInput,
    StepOutput,
    StepPaused,
    StepStarted,
    StreamEvent,
    ToolCallsRequired,
)

# Result models
from ._models.responses import (
    ExecuteResult,
    JobAccepted,
    JobStatus,
    PausedResult,
)

# Trace models
from ._models.trace import (
    RunSummary,
    StepAttempts,
    StepTrace,
    TokenBreakdown,
    Trace,
)
from ._run import AsyncRun, Run
from ._trace_scope import current_session_id, trace, trace_scope, trace_scope_sync
from ._version import __version__

__all__ = [
    "__version__",
    # Clients
    "Noukai",
    "AsyncNoukai",
    # Proxies
    "Flow",
    "AsyncFlow",
    "Run",
    "AsyncRun",
    "Job",
    "AsyncJob",
    # Results
    "ExecuteResult",
    "PausedResult",
    "JobAccepted",
    "JobStatus",
    # Events
    "StreamEvent",
    "RunStarted",
    "StepStarted",
    "StepInput",
    "StepOutput",
    "StepCompleted",
    "StepFailed",
    "StepPaused",
    "ToolCallsRequired",
    "FlowCompleted",
    # Trace
    "Trace",
    "RunSummary",
    "StepTrace",
    "StepAttempts",
    "TokenBreakdown",
    # Errors
    "NoukaiError",
    "APIConnectionError",
    "APITimeoutError",
    "AuthenticationError",
    "PermissionDeniedError",
    "FlowNotFoundError",
    "InsufficientCreditsError",
    "RateLimitError",
    "FlowExecutionError",
    "ToolCallLimitError",
    # Replay errors
    "ReplayError",
    "ReplayDisabledError",
    "ReplayForbiddenError",
    "ReplayInvalidSessionError",
    "ReplayLeftoverError",
    "ReplayMissError",
    "ReplayNoSnapshotsError",
    "ReplaySessionExpiredError",
    "ReplaySessionNotFoundError",
    # Replay scope
    "trace",
    "trace_scope",
    "trace_scope_sync",
    "current_session_id",
]
