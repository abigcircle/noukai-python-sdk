# Changelog

All notable changes to this project will be documented in this file. The
format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] — 2026-09-16

### Added

- **Opt-in customer-side OpenTelemetry** (design `20260916-SDK-otel-and-replay-rename`).
  `Noukai(..., otel=True)` emits one span of kind `CLIENT` per `flow.execute()` /
  `flow.execute_async()` call into the caller's own configured OpenTelemetry
  provider — `noukai.flow.execute` / `noukai.flow.execute_async`, with
  `noukai.org`/`project`/`flow.slug`/`flow.version`/`execution_id`/`flow.status`
  attributes, `ERROR` status + recorded exception on failure. Off by default and
  a **true no-op** when off (the SDK never imports `opentelemetry` unless opted
  in). Requires the new `[otel]` extra (`pip install noukai-sdk[otel]`, depends
  on `opentelemetry-api` only). Pass a custom tracer with `tracer=` to override
  the global provider.
- **Per-pipeline-block child spans** (opt-in). `Noukai(..., otel_steps=True)`
  additionally fetches `run.trace()` after a completed `execute` and emits one
  backdated child span per block, nested under the call span, carrying
  `gen_ai.request.model`, `gen_ai.usage.input_tokens`/`output_tokens`,
  `noukai.step.cost_usd`/`id`/`status`/`duration_ms`. `otel_step_payloads=True`
  additionally attaches each block's **input data and output results** (plus the
  error context for failed blocks) — size-bounded; off by default (may contain
  PII). The trace fetch is best-effort, so a fetch failure never breaks the
  call. `traceparent`
  propagation and spans on the streaming `steps()`/`events()` calls remain
  deferred follow-ups.

### Changed

- **BREAKING — the replay/capture scope is renamed `trace*` → `replay*`**
  (design `20260916-SDK-otel-and-replay-rename`). The scope was misleadingly
  named `trace`, colliding with the execution-trace API (`run.trace()`) and the
  `trace=` snapshot-capture param. This is a hard rename — no deprecation alias.
  - `trace` (decorator) → `replay`
  - `trace_scope` → `replay_scope`
  - `trace_scope_sync` → `replay_scope_sync`
  - internal module `_trace_scope.py` → `_replay_scope.py`

  `current_session_id` is unchanged, and the execution-trace API
  (`run.trace()` / `.step_trace()` / `.live_trace()`, the `Trace`/`StepTrace`
  models) and the `execute(..., trace=...)` capture param are untouched.

  **Migration:**
  ```python
  # before
  from noukai_sdk import trace, trace_scope, trace_scope_sync
  # after
  from noukai_sdk import replay, replay_scope, replay_scope_sync
  ```
  The FastAPI/Flask adapter surface (`NoukaiTraceMiddleware`, `init_noukai_trace`)
  is intentionally unchanged in this release.

- Version re-synced with `@noukai/sdk` at `0.5.0` (the repos had drifted at
  Python `0.4.0` / TypeScript `0.4.1`).

## [0.4.0] — 2026-09-14

### Added

- **`messages` on the request model (F6)** — `flow.execute()` (sync + async)
  accepts a structured `messages: list[ChatMessage] | None`, mutually exclusive
  with `message`, for chat/agent flows (design `20260903-SDK-agent-relay`, PR-2).
  A new permissive `ChatMessage` model (`role`/`content`/`tool_calls`/
  `tool_call_id`/`name`, `extra="allow"`) tracks `llm_service.models.ChatMessage`.
  `ChatMessage` is exported at the top level. This brings the SDK request model
  back in sync with the server's `SeqflowExecuteRequest`.
- **Wire contents are camelCase (camelCase alignment).** `ChatMessage` keeps
  snake_case Python attributes (`tool_calls`/`tool_call_id`) but adds camelCase
  wire aliases (`toolCalls`/`toolCallId`) with `populate_by_name=True`, so
  `model_dump(by_alias=True)` (the serialization the transport uses) emits
  camelCase. The whole Noukai wire — envelope and message/tool contents — is now
  camelCase, matching the router-ai-slugs execute API; snake_case lives only at
  the external LLM-provider boundary. Callers building tool-result messages for a
  resume should use `{"role": "tool", "toolCallId": ..., "content": ...}`.
- **Client-side validation of the fresh-call contract** — `message`/`messages`
  are rejected together (`ValueError`), `messages[]` roles are restricted to
  `user`/`assistant`/`tool` (a `system`/`function` turn raises before the wire),
  and a one-time `UserWarning` fires as a `messages` payload approaches the
  server's 1 MB cap (`MESSAGES_TOO_LARGE`).
- **Execute-transport seam** — a transport-pluggable `ExecuteTransport`
  (`send(payload) -> (status, body)`) protocol (+ sync mirror) makes the
  yield/resume tool-calling loop reusable across transports. `flow.execute()`
  now routes resume through a `DirectExecuteTransport` (today's key-holding
  behavior, unchanged — the existing tool-call tests are the regression guard).
- **Keyless relay entrypoint** — `RelayFlow(url)` / `AsyncRelayFlow(url)` run the
  **same** loop over a `RelayExecuteTransport` that POSTs the raw payload to a
  relay URL with no `nk_` key and no `/seq` path (the browser / server-to-server
  / CLI agent position). Both fresh-call modes (`message` and `messages`) are
  supported. `RelayFlow`, `AsyncRelayFlow`, `RelayExecuteTransport`, and
  `RelaySyncExecuteTransport` are exported at the top level.

- **Verbatim relay transport mode + relay adapter** (design
  `20260903-SDK-agent-relay`, PR-1) — a browser or any keyless client drives a
  tool-calling flow without ever seeing your `nk_` key; your server is a thin
  keyholder proxy. `AsyncTransport.request()` / `SyncTransport.request()` gain
  `raise_for_status: bool = True` (when `False`, a non-2xx is returned as a
  `Response` rather than raising — idempotent retries still apply). New subpath
  adapter `noukai_sdk.adapters.relay` with `mount_flow_relay(...)`
  (FastAPI/Starlette) and `flow_relay_blueprint(...)` (Flask); `RelayBounds` is
  exported. The relay bounds the raw body before parse (`413`), bounds message
  counts, rejects malformed JSON (`400`), awaits your `authorize` hook (raise to
  reject), then forwards verbatim to `/seq/{org}/{project}/{slug}/execute` with
  the `nk_` bearer injected, relaying the upstream `(status, body)` verbatim
  (non-JSON → `{"detail": "UPSTREAM_NON_JSON"}`). It never logs the key or the body.

### Changed

- **Client round limit reconciled to one value.** The keyless relay loop uses
  the SDK's `DEFAULT_MAX_TOOL_ROUNDS` (**10**), reconciling the two historical
  limits (SDK `10` vs the extracted `@noukai/agent`'s `12`). When
  `@noukai/agent` is re-expressed over this loop (PR-3), its effective limit
  becomes `10` — a deliberate, documented one-round-fewer change for that path.

### Fixed

Post-review parity and correctness fixes (design `20260903-SDK-agent-relay`),
landed with the matching TypeScript fixes in lockstep:

- **Empty `messages=[]` is omitted from the wire** (matches TS), and a
  `messages` entry missing a `role` is rejected in `validate_fresh_call` with a
  clear message rather than only later at model construction.
- **The `messages` size soft-warning is a `UserWarning`** (was `ResourceWarning`,
  which the default filter suppresses) so it is visible like the TS peer's
  `console.warn`.
- **The Flask relay blueprint uses a unique name** derived from the pinned flow,
  so one app can mount more than one relay (the FastAPI side already supported
  this; a duplicate blueprint name previously crashed app startup).
- **Relay body read errors return `400 INVALID_JSON`** on a mid-stream client
  disconnect / malformed chunk instead of an unhandled framework `500`, matching
  the TS relay.
- **`run_started` / `step_paused` stream events are no longer dropped**
  (independent of the relay work; surfaced by the integration suite). The server
  keys these frames by `executionId` and no longer emits `runId` / `stepId`, so
  `RunStarted.run_id` and `StepPaused.step_id` are now optional (mirroring
  `FlowCompleted`). Previously the frames failed validation and were silently
  skipped, so `Flow.events()` never surfaced `RunStarted` and `Flow.steps()`
  stalled after the first step (the dropped `step_paused` never triggered the
  next `/step`). The run/step identity is `execution_id`.

### Known limitations

- **Header-derived error metadata is `None` on the relay path.** The
  execute-transport seam is `send(payload) -> (status, body)` and carries no
  HTTP headers, so errors surfaced through a `RelayFlow` / `AsyncRelayFlow`
  (or any `ExecuteTransport`) have `RateLimitError.retry_after`,
  `AuthenticationError.www_authenticate`, and `NoukaiError.request_id` set to
  `None`. The direct `flow.execute()` path still reads these from the response
  headers. This is intentional — the seam is deliberately not widened to carry
  headers.

## [0.3.0] — 2026-07-06

### Breaking

- `StepStarted.step_index`, `StepPaused.step_index`, and
  `StepCompleted.step_index` are now **required** and **guaranteed to be
  flow-absolute, consumer-frame indices** stamped by the SDK before each
  event is yielded. Previously: optional, and (when present from the server)
  segment-local — every `/step` call's events restarted at `0`, leaking the
  SDK's pause/resume transport segmentation. Consumers relying on
  `event.step_index or fallback` can drop the fallback. The new contract
  holds for both live SSE (async + sync iterators) and replay-mode
  reconstruction. `step_paused.step_index` reports the index of the
  just-completed step (the pause is "for" that step), matching the
  `step_completed` that precedes it.
- `StepCompleted` gains a `step_index: int` field (previously absent from
  the wire and the model).

### Fixed

- Replay reconstruction now strips reserved trace sidecar keys (currently
  `__rendered_prompt__`) from `output_snapshot` before surfacing them on
  `StepCompleted.output`, `FlowCompleted.result`, and `ExecuteResult.output`.
  Previously the replayed shape was a superset of the live-execution shape
  (which excludes those keys), so round-trip equality checks between live
  and replay results could fail. New helper:
  `noukai_sdk.replay.snapshot.strip_trace_sidecars`.

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
