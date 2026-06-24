# Changelog

All notable changes to this project will be documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-06-06

### Added

- `@noukai_sdk.trace` decorator and `trace_scope` / `trace_scope_sync` context
  managers. Wrapping a route (or any callable) in `@trace` groups every Noukai
  SDK call it makes under a single session id so the full execution can be
  replayed later.
- `current_session_id() -> str | None` — returns the session id of the active
  scope, or `None` when called outside a scope.
- `session_id=` optional kwarg on `Flow.execute()`, `Flow.steps()`,
  `Flow.events()`, `Flow.execute_async()`, and their `AsyncFlow` counterparts.
  When passed outside a scope the SDK sends `X-Session-Id` on the wire;
  when passed *inside* an active replay scope it triggers a one-shot fetch
  of that explicit session instead of drawing from the scope cassette (see
  Caveats in README).
- `session_id=` optional kwarg on `Noukai(...)` / `AsyncNoukai(...)` — sets a
  default session id for every call made through that client.
- `ExecuteResult.session_id` property surfaces the captured or replayed session
  id returned in the `X-Noukai-Session` response header.
- **Replay mode.** When `NOUKAI_REPLAY_ENABLED=true` and the caller passes
  `X-Noukai-Replay: <session_id>` to the adapter, the SDK fetches the recorded
  session via `GET /seq/sessions/{id}` (idempotent; retried by default transport
  retry logic — see R3 note in README) and serves each `Flow.execute()` /
  `steps()` / `events()` call from the cassette instead of making live calls.
- **Framework adapters:**
  - `noukai_sdk.adapters.fastapi.NoukaiTraceMiddleware` — ASGI middleware for
    FastAPI / Starlette; reads `X-Noukai-Replay`, opens/closes a trace scope
    around the request, and writes `X-Noukai-Session` to the response.
  - `noukai_sdk.adapters.flask.init_noukai_trace` — Flask before/after-request
    hooks equivalent.
- **9 replay error classes** (all extend `ReplayError` which extends
  `NoukaiError`):
  - `ReplayError` — base class for all replay errors.
  - `ReplayMissError` — no matching execution found in the cassette for a
    `(slug, position)` lookup.
  - `ReplayLeftoverError` — scope exited with unconsumed executions remaining
    in the cassette.
  - `ReplayForbiddenError` — replay attempted against a session that belongs to
    a different org/project.
  - `ReplaySessionNotFoundError` — the requested session id does not exist on
    the backend.
  - `ReplaySessionExpiredError` — the session exists but its TTL has elapsed and
    snapshots are no longer available.
  - `ReplayInvalidSessionError` — the session payload is structurally invalid or
    cannot be parsed.
  - `ReplayNoSnapshotsError` — the session exists but `trace_capture_mode` was
    `off` so no snapshot data was recorded.
  - `ReplayDisabledError` — replay was requested but `NOUKAI_REPLAY_ENABLED` is
    not set to `true` (raised only when the adapter is used without the guard).
- Optional package extras: `noukai-sdk[fastapi]` (pulls FastAPI + Starlette)
  and `noukai-sdk[flask]` (pulls `flask>=3.0`).
- Centralized URL audit registry at `noukai_sdk/_paths.py` — single file
  lists every backend route the SDK calls. Auditing the wire surface is one
  file read.

### Fixed

- Replay session fetch now hits `GET /seq/sessions/{id}` (was incorrectly
  `GET /sessions/{id}` — 404 in production).
- Replay matcher now compares against the BE's bare `flow.slug` (e.g.
  `"grade-3"`), not the synthesized `org/project/slug` that fixtures
  previously used. Replay against the real backend now actually matches
  recorded executions.
- `SessionExecution` model aligned with BE schema: `status` includes
  `"pending"` and `"cancelled"`; `flow_id`, `slug`, `trigger_type`,
  `trace_capture_mode`, and `error_at_step` are now Optional so the SDK
  does not crash when the underlying flow has been deleted.
- Reserved-header guard on `extra_headers=`: a misconfigured caller cannot
  overwrite `Authorization`, `X-Noukai-API-Version`, `User-Agent`,
  `X-Request-ID`, `Content-Type`, or `Cookie` via per-request headers.
- Unified `Flow.execute() / steps() / events()` REPLAY-mode dispatch — all
  three now apply the same rule when an explicit `session_id` matches or
  differs from the scope. Previously `events()` and `steps()` made a live
  call for explicit-sid-matching-scope, asymmetric with `execute()`.
- Session-id precedence chain uses `is not None` instead of `or`, so an
  explicit empty-string `session_id=""` is no longer silently overridden
  by the next tier.

### Internal

- Transport `request()` and `stream()` accept an `extra_headers=` kwarg; used
  by the replay subsystem to inject `X-Session-Id` / `X-Noukai-Replay` without
  touching the public client surface.
- Log handler receives `scope_open` and `scope_close` events with `mode`
  (`"normal"` | `"capture"` | `"replay"`) and `session_id` fields.

### Requires

- Backend session-grouping endpoint per BE design
  `20260605-BE-execution-session-grouping` (`GET /seq/sessions/{id}`,
  `X-Session-Id` header on `/execute` + `/step` routes).

## [0.1.0] — 2026-05-31

### Added
- Initial release.
- `Noukai` and `AsyncNoukai` clients (sync + async).
- `flow.execute()`, `flow.execute_async()`, `flow.steps()`, `flow.events()`.
- `flow.run(id).trace()`, `step_trace()`, `live_trace()`.
- Tool-call auto-resume via `tool_handler=`; manual mode via `PausedResult.resume()`.
- Typed Pydantic event hierarchy for SSE streams.
- Exception hierarchy mapped to HTTP status; server error codes on `.code`.

[Unreleased]: https://github.com/noukai/noukai-python/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/noukai/noukai-python/releases/tag/v0.1.0
