"""Wire-level constants. Mirrors server's noutai-queue/flow_run_trace + seqflow models."""

from typing import Final

DEFAULT_BASE_URL: Final = "https://api.noukai.xyz/api/v1"
DEV_BASE_URL: Final = "http://localhost:8080/api/v1"
DEFAULT_TIMEOUT_SECONDS: Final = 300.0
DEFAULT_JOB_POLL_REQUEST_TIMEOUT_SECONDS: Final = 30.0
DEFAULT_JOB_POLL_INTERVAL_SECONDS: Final = 2.0
DEFAULT_JOB_WAIT_TIMEOUT_SECONDS: Final = 300.0
DEFAULT_MAX_RETRIES: Final = 1
DEFAULT_MAX_TOOL_ROUNDS: Final = 10

# Soft warning threshold for the accumulated `tool_call_messages` list
# (model request + every round of tool results). When the next resume would
# push the list past this length, the SDK emits a one-time
# ``ResourceWarning`` advising the caller — the server will eventually reject
# the request with ``MESSAGES_TOO_LARGE``. Hard ceiling is server-side.
TOOL_CALL_MESSAGES_SOFT_LIMIT: Final = 200

API_KEY_PREFIX: Final = "nk_"
API_KEY_ENV_VAR: Final = "NOUKAI_API_KEY"
ENV_ENV_VAR: Final = "NOUKAI_ENV"

# Headers
HEADER_API_VERSION: Final = "X-Noukai-API-Version"
HEADER_REQUEST_ID: Final = "X-Request-ID"
HEADER_USER_AGENT: Final = "User-Agent"

# Replay feature headers (see design 20260605-SDK-replay-decorator)
# HEADER_SESSION_ID    — SDK → backend (capture: tag the flow_run with the session)
# HEADER_REPLAY        — caller's HTTP client → user's route (with session id payload)
# HEADER_RESPONSE_SESSION — user's framework adapter → caller (advertises the captured session id)
HEADER_SESSION_ID: Final = "X-Session-Id"
HEADER_REPLAY: Final = "X-Noukai-Replay"
HEADER_RESPONSE_SESSION: Final = "X-Noukai-Session"

# Env var that gates honoring X-Noukai-Replay (production safety)
REPLAY_ENABLED_ENV_VAR: Final = "NOUKAI_REPLAY_ENABLED"

# Pin the API contract this SDK was built against
API_VERSION: Final = "2026-05-31"


class TraceEventType:
    """Mirror of server's flow_run_trace.TraceEventType."""

    FLOW_STARTED: Final = "flow_started"
    RUN_STARTED: Final = "run_started"  # legacy; see CONTEXT.md gotcha #10
    STEP_STARTED: Final = "step_started"
    STEP_INPUT: Final = "step_input"
    STEP_OUTPUT: Final = "step_output"
    STEP_COMPLETED: Final = "step_completed"
    STEP_ERROR: Final = "step_error"
    STEP_PAUSED: Final = "step_paused"
    STEP_PROGRESS: Final = "step_progress"
    LOOP_COMPLETED: Final = "loop_completed"
    FLOW_COMPLETED: Final = "flow_completed"


class ServerErrorCode:
    """Mirror of server's SeqflowExecuteErrorCode + SeqflowStepErrorCode union.
    Lands on NoukaiError.code attribute for users to branch on.
    """

    # /seq/.../execute
    FLOW_NOT_FOUND: Final = "FLOW_NOT_FOUND"
    INVALID_TREE: Final = "INVALID_TREE"
    NO_STEPS: Final = "NO_STEPS"
    INSUFFICIENT_CREDITS: Final = "INSUFFICIENT_CREDITS"
    CREDITS_EXHAUSTED: Final = "CREDITS_EXHAUSTED"
    BILLING_UNAVAILABLE: Final = "BILLING_UNAVAILABLE"
    INTERNAL_ERROR: Final = "INTERNAL_ERROR"
    # Tool-call related
    TOOLS_NOT_ENABLED: Final = "TOOLS_NOT_ENABLED"
    TOOLS_INVALID: Final = "TOOLS_INVALID"
    TOOL_NAME_INVALID: Final = "TOOL_NAME_INVALID"
    TOOLS_IN_NON_SEQUENTIAL_STEP: Final = "TOOLS_IN_NON_SEQUENTIAL_STEP"
    PAUSED_STEP_INVALID: Final = "PAUSED_STEP_INVALID"
    EXECUTION_ID_INVALID: Final = "EXECUTION_ID_INVALID"
    TOOL_RESULTS_MISMATCH: Final = "TOOL_RESULTS_MISMATCH"
    TOOL_ITERATION_LIMIT: Final = "TOOL_ITERATION_LIMIT"
    MESSAGES_TOO_LARGE: Final = "MESSAGES_TOO_LARGE"
    TOOLS_REQUIRE_SYNC_EXECUTE: Final = "TOOLS_REQUIRE_SYNC_EXECUTE"
    # /seq/.../step extras
    INVALID_STEP_INDEX: Final = "INVALID_STEP_INDEX"
    INVALID_FIRST_CALL: Final = "INVALID_FIRST_CALL"
    STALE_TREE: Final = "STALE_TREE"
    MISSING_MESSAGE: Final = "MISSING_MESSAGE"
    RUN_NOT_FOUND: Final = "RUN_NOT_FOUND"
    # BYOK
    BYOK_KEY_REJECTED: Final = "BYOK_KEY_REJECTED"
