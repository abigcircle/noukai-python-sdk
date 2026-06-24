# noukai_sdk — Python SDK

## Purpose

Public Python SDK for executing Noukai flows from external applications. Provides sync (`Noukai`) and async (`AsyncNoukai`) clients, fully typed with `py.typed`, Pydantic models for results and events, and framework adapters for FastAPI and Flask.

## Package layout

```
src/noukai_sdk/
  __init__.py            # Public surface re-exports — all public API lives here
  _client.py             # Noukai / AsyncNoukai client constructors
  _flow.py               # Flow / AsyncFlow proxies (flow.execute, steps, events, execute_async)
  _run.py                # Run / AsyncRun proxies (run.trace, step_trace, live_trace)
  _jobs.py               # Job / AsyncJob proxies (job.wait, poll)
  _transport.py          # AsyncTransport — httpx-backed HTTP + SSE
  _transport_shared.py   # TransportConfig and shared retry/backoff logic
  _streaming.py          # SSE event parsing and typed stream helpers
  _step_iterator.py      # Step iterator (filters SSE stream to StepCompleted events)
  _tool_calls.py         # Auto-resume tool-call loop
  _errors.py             # Full exception hierarchy (NoukaiError subclasses + replay errors)
  _models/
    events.py            # Typed SSE event models (RunStarted, StepCompleted, …)
    responses.py         # ExecuteResult, PausedResult, JobAccepted, JobStatus
    trace.py             # Trace, RunSummary, StepTrace, StepAttempts, TokenBreakdown
  _trace_scope.py        # trace decorator, trace_scope/trace_scope_sync CMs, current_session_id()
  _version.py            # __version__ string
  py.typed               # PEP 561 marker
  replay/
    __init__.py          # Re-exports ScopeState, SessionResponse (internal types)
    _state.py            # ContextVar-based ScopeState; active-scope bookkeeping
    fetcher.py           # Fetches GET /seq/sessions/{id} via transport
    matcher.py           # Matches (slug, position) pairs to cassette executions
    sse_reconstructor.py # Reconstructs SSE event stream from a replay snapshot
  adapters/
    __init__.py          # Empty — adapters are subpath imports only
    fastapi.py           # NoukaiTraceMiddleware (ASGI, streaming-safe)
    flask.py             # init_noukai_trace (before/after-request hooks)
```

## Public surface

All public exports are declared in `__init__.py`:

- **Clients:** `Noukai`, `AsyncNoukai`
- **Proxies:** `Flow`, `AsyncFlow`, `Run`, `AsyncRun`, `Job`, `AsyncJob`
- **Results:** `ExecuteResult`, `PausedResult`, `JobAccepted`, `JobStatus`
- **Events:** `StreamEvent`, `RunStarted`, `StepStarted`, `StepInput`, `StepOutput`, `StepCompleted`, `StepFailed`, `StepPaused`, `ToolCallsRequired`, `FlowCompleted`
- **Trace models:** `Trace`, `RunSummary`, `StepTrace`, `StepAttempts`, `TokenBreakdown`
- **Errors:** `NoukaiError` + 9 subclasses + `ReplayError` + 8 replay subclasses
- **Replay scope:** `trace`, `trace_scope`, `trace_scope_sync`, `current_session_id`
- **Adapters** (subpath imports only, not in `__init__.py`):
  - `from noukai_sdk.adapters.fastapi import NoukaiTraceMiddleware`
  - `from noukai_sdk.adapters.flask import init_noukai_trace`

## Replay subsystem (added 0.2.0)

Implemented in `_trace_scope.py` + `replay/`. Allows grouping multiple SDK
calls under one session id (capture) and later replaying recorded responses
deterministically (replay).

**Capture mode:** always active when `@trace` / `trace_scope` is used. The SDK
injects `X-Session-Id` on every outbound request.

**Replay mode:** activated when `NOUKAI_REPLAY_ENABLED=true` AND the adapter
(or caller) supplies a `replay_session_id`. The SDK fetches
`GET /seq/sessions/{id}` (idempotent; retried by transport default retry policy)
and serves `Flow.execute()` / `steps()` / `events()` from the cassette.

**Backend dependency:** requires the session-grouping endpoint from BE design
`20260605-BE-execution-session-grouping`:
- `X-Session-Id` accepted on `/execute` and `/step` routes.
- `GET /seq/sessions/{id}` returns `SessionResponse` JSON.

**Env var:** `NOUKAI_REPLAY_ENABLED=true` gates replay. Without it, the
`X-Noukai-Replay` header is silently ignored and the scope opens in capture
mode (safe to deploy to production).

## Dependencies

Runtime: `httpx>=0.27`, `pydantic>=2.6`, `typing-extensions>=4.10` (Python <3.11).
Optional extras: `noukai-sdk[fastapi]` (`starlette>=0.36`), `noukai-sdk[flask]` (`flask>=3.0`).

## Version

Current: `0.2.0`. Semantic versioning; 0.x minor bumps for feature additions.
