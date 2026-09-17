# 20260916-SDK-otel-and-replay-rename — Customer-side OpenTelemetry + `trace*`→`replay*` rename

- **Status:** Draft — awaiting developer approval to commit as a design PR.
- **Date:** 2026-09-16
- **Author:** kii (with Claude)
- **Scope:** ships to **BOTH** SDKs together — `noukai-python-sdk` (`noukai-sdk`) **and** `noukai-typescript-sdk` (`@noukai/sdk`). Every PR lands in both, at the same version (see **Dual-land requirement**).
- **Design-doc convention:** referenced from code/CHANGELOG as `20260916-SDK-otel-and-replay-rename` (matches `20260903-SDK-agent-relay`, `20260605-SDK-replay-decorator`).
- **Decisions settled up front (do not re-litigate):** hard rename (no deprecation alias); OTel v1 = parent CLIENT span only; opt-in via a `otel=True` client flag (with a `tracer=` override); both repos bump to `0.5.0`.

> **Repo note:** the parent `noukai-sdk/` is not itself a git repo — the two SDKs are. This doc lives at the parent so it can speak for both, and is mirrored into each repo's `docs/design-logs/2026/09Sep/` so it is version-controlled with the code (same pattern as `20260903-SDK-agent-relay`).

---

## TL;DR

Two independent changes ship together as one coordinated release (`0.5.0`), because both touch the replay/trace surface and both must stay mirrored across the two SDKs:

1. **Customer-side OpenTelemetry (opt-in).** When a caller sets `otel=True`, each `execute` / `execute_async` / `steps` / `events` call emits **one parent OTel span of kind CLIENT** (`noukai.flow.execute`, etc.) into *the caller's own* OTel provider (Datadog / Honeycomb / Jaeger / any OTLP backend). No server work, no dependency on Noukai-server OTel, no new data — the span carries org/project/slug/execution_id/status/duration the SDK already has. When `otel` is off (the default) the SDK **never imports OpenTelemetry** and the path is a true no-op.

2. **Rename the misnamed replay scope `trace*` → `replay*`.** The capture/replay scope is confusingly named `trace`, colliding with two *correctly*-named neighbours: the execution-trace API (`run.trace()` → `StepTrace[]`, which OTel is built on) and the `trace: bool` snapshot-capture param. We hard-rename the scope only: `trace`→`replay`, `trace_scope`→`replay_scope`, `trace_scope_sync`→`replay_scope_sync` (Python); `traceScope`→`replayScope`, `TraceScopeOptions`→`ReplayScopeOptions` (TS). Breaking; migration note + CHANGELOG in both.

Deferred to a follow-on **PR-C** (explicitly out of v1, documented below): per-step child spans reconstructed from `run.trace()`, and W3C `traceparent` header injection for a future server-side continuation.

---

## Dual-land requirement & parity contract

This is a **two-SDK change by construction.** Both changes exist so Python servers *and* TypeScript/Node apps get the identical capability and an identical public surface.

1. **Every logical PR ships Python + TypeScript together** and is not done until both are implemented, tested, README-documented, and example-covered. No "TS-first, Python later." (Because the two SDKs are *separate git repos*, one logical PR is physically two coordinated PRs — one per repo — landed as a pair.)
2. **Same released `CHANGELOG.md` version in both repos.** `noukai-python-sdk/scripts/check_parity.py` compares the top released `## [X.Y.Z]` heading across the two repos and fails on drift. **The repos are currently drifted (Py `0.4.0`, TS `0.4.1` — a TS-only browser-safety hotfix); this release re-syncs both onto `0.5.0`.**
3. **Async/sync symbol parity holds** within Python (`tests/unit/test_parity.py`: any public method on `AsyncNoukai`/`AsyncFlow`/`AsyncRun` has a sync twin, modulo `aclose`/`close`). The OTel wrapping must preserve this — both sync and async execute paths get the span.
4. **Structural mirror across languages.** The SDKs are file-for-file mirrors (`_transport.py`↔`transport.ts`, `_flow.py`↔`flow.ts`, `_run.py`↔`run.ts`, `_trace_scope.py`↔`replay/scope.ts`). New files mirror: `_otel.py` ↔ `otel.ts`. The rename keeps the mirror (`_trace_scope.py`→`_replay_scope.py` ↔ `replay/scope.ts` exports renamed in place).

Read every PR below as "do this in Python **and** TypeScript."

---

## Background: what exists today

### The three unrelated things called "trace"

| Concept | Python surface | TS surface | Correctly named? |
|---|---|---|---|
| **(a) Replay/capture scope** | `@trace`, `trace_scope`, `trace_scope_sync` (`_trace_scope.py`) | `traceScope`, `TraceScopeOptions` (`replay/scope.ts`) | **No** — it establishes capture/replay mode; "trace" is misleading |
| **(b) Execution-trace API** | `run.trace()`, `.step_trace()`, `.live_trace()`, models `Trace`/`StepTrace` (`_run.py`, `_models/trace.py`) | `run.trace()`, `.stepTrace()`, `.liveTrace()`, `types/trace.ts` | **Yes** — it fetches the execution's step trace |
| **(c) Snapshot-capture flag** | `execute(..., trace: bool = False)` | `execute({ trace?: boolean })` | Acceptable — it's the server's `ExecuteRequest.trace` wire field |

The rename fixes **(a)** only. OTel is built on **(b)** (today for the deferred per-step spans; the parent span needs nothing but the call itself). **(c)** is a server wire-field name and is left as-is (see Non-goals / Decisions).

### Telemetry that exists today

Both SDKs already have a **logging hook** — Python `Noukai(log_handler=fn, log_payloads=False)`, TS `new Noukai({ onLog, logPayloads:false })` — emitting `{phase, method, path, statusCode, requestId, attempt}` plus `scope_open`/`scope_close`. This is structured logging, **not** distributed tracing, and stays exactly as-is. OTel is additive and orthogonal.

### Where the wiring points are (verified)

- **Parent span host:** `Flow.execute` / `execute_async` (`_flow.py:156,310` sync class; `:644,772` async class), `steps`/`events` (delegate to `_step_iterator.py`). TS: `Flow.execute`/`executeAsync`/`steps`/`events` (`flow.ts:127,256,338,354`). `execution_id` is available post-call from `ExecuteResult.execution_id` (`str | None`), `PausedResult.execution_id`, `JobAccepted.execution_id`.
- **Opt-in host:** `Noukai`/`AsyncNoukai.__init__` (`_client.py:208,321`), TS `Noukai` constructor + `NoukaiOptions` (`client.ts:17,152`). Transport is built there and passed to every `Flow`.
- **Deferred `traceparent` seam:** the transport already merges caller `extra_headers` and its reserved-header allowlist does **not** include `traceparent`, so injection is a clean future add (`_transport.py:55`, `transport.ts:123`).

---

## Motivation

- **Customers running the SDK have no timeline view of their Noukai calls.** The per-run data (timing, tokens, cost, model) is already fetchable via `run.trace()` and visible in the Noukai UI, but it doesn't show up in the customer's *own* observability stack alongside their DB queries and HTTP calls. A single CLIENT span per flow call closes that gap with near-zero cost and no server changes.
- **The `trace` scope name is an active foot-gun.** New readers conflate the replay scope with `run.trace()` and the `trace=` capture flag. Renaming to `replay*` makes the three concepts self-describing — and it's cheapest to do *now*, in the same breaking release as OTel, rather than paying a second breaking bump later.
- **We are reversing a v1 scoping note, deliberately and narrowly.** The `20260531-SDK-python-sdk-v1` brief deferred "OpenTelemetry instrumentation… to v1.x once feedback arrives," and `20260706-BE-grafana-observability` chose "no traces" *server-side*. This design is the v1.x follow-through for the **client** axis only; it does not add server/Tempo tracing. (Called out again in *Decisions made*.)

---

## Goals / Non-goals

**Goals**
1. One **opt-in** parent CLIENT span per `execute` / `execute_async` call, emitted into the caller's OTel provider, following OTel semantic conventions (span kind CLIENT; `gen_ai.*` reserved for the deferred per-step spans). Streaming `steps()` / `events()` calls are **not** span-wrapped in v1 (see Non-goals).
2. **True no-op when off:** default `otel=False` never imports `opentelemetry`; no measurable overhead; no hard dependency added to either SDK.
3. **Hard-rename** the replay scope `trace*`→`replay*` in both SDKs, mirrored, with migration notes.
4. **Re-sync parity** — both repos to `0.5.0`, `check_parity.py` green.
5. Preserve every existing behavior: logging hook, replay/capture, tool-call resume, sync/async parity — all unchanged.

**Non-goals (v1 — deferred to PR-C, documented not built)**
- **Per-step child spans** reconstructed from `run.trace()` `StepTrace[]` using backdated start/end timestamps (nested under the parent, one span per step, `gen_ai.*` + cost attributes). Requires the run to be complete → an extra `GET …/trace` for the sync path. Designed below; built when prioritized.
- **W3C `traceparent` injection** into the outbound execute request for a future server-side continuation. Cheap and flag-gated, but no server consumer exists yet.
- **Spans on the streaming `steps()` / `events()` calls.** Correctly bounding a span across a caller-driven, lazily-consumed iterator (open on first pull; close on exhaustion / early-break / error without disturbing the SSE-stream teardown) shares the same span-lifetime machinery as the per-step children, so it is deferred with them to PR-C. v1 spans only the request-based `execute` / `execute_async` entry points.
- **Renaming the `trace: bool` execute param** (e.g. to `capture=`/`snapshot=`). It's a server wire-field name; changing it is a separate wire-adjacent decision. Flagged for a future design.
- No server-side OTel, no Tempo, no dependency on Noukai-server tracing.
- No deprecation-alias shim for the rename (hard rename — decided).

---

## Design overview

### 1) The opt-in seam (zero-overhead when off)

A single new internal module per SDK owns *all* OpenTelemetry contact, so the rest of the SDK never imports `opentelemetry` directly and the "off" path is provably a no-op.

- **Python** — `src/noukai_sdk/_otel.py`. Exposes a `Tracer`-like facade `get_flow_tracer(enabled: bool, tracer: object | None) -> _SpanFactory`. When `enabled` is `False` it returns a `_NoopSpanFactory` whose `flow_span(...)` is a null context manager — **no `import opentelemetry` happens**. When `enabled` is `True` it does a guarded `from opentelemetry import trace` inside the function; if the package is missing it raises a crisp `NoukaiError` ("`otel=True` requires the `[otel]` extra: `pip install noukai-sdk[otel]`"). An explicit `tracer=` overrides provider acquisition.
- **TypeScript** — `src/otel.ts`. Mirror: `getFlowTracer(enabled, tracer?)`. Off → a no-op factory (never `import("@opentelemetry/api")`). On → `await import("@opentelemetry/api")` (dynamic, so bundlers don't hard-require it), throwing a clear error if the optional peer dep is absent. `@opentelemetry/api` added as an **optional peerDependency** (`peerDependenciesMeta.optional`), never a runtime dep.

Client wiring:
```
Noukai(otel=True)                 # Python: uses the globally-configured provider's tracer
Noukai(otel=True, tracer=my_tr)   # explicit tracer override (skips global lookup)
new Noukai({ otel: true })        # TS
new Noukai({ otel: true, tracer })
```
The client stashes a `_span_factory` on the transport (alongside the existing `_default_session_id`) so every `Flow` proxy reaches it with no signature churn.

### 2) The parent span

Each `execute` / `execute_async` call is wrapped in one span (streaming `steps()` / `events()` are deferred — see Non-goals):

| Aspect | Value |
|---|---|
| **Name** | `noukai.flow.execute`, `noukai.flow.execute_async` |
| **Kind** | `CLIENT` |
| **Attributes (set at start)** | `noukai.org`, `noukai.project`, `noukai.flow.slug`, `noukai.flow.version` |
| **Attributes (set at end)** | `noukai.execution_id` (when known), `noukai.flow.status` (`completed` / `failed` / `tool_calls_required`; **unset** on the `execute_async` submission — the returned `Job` handle carries no status) |
| **Status** | `OK` on success; on exception → `record_exception(e)` + `set_status(ERROR)` then re-raise (recorded exactly once — the tracer's own auto-recording is disabled) |
| **Lifetime** | Around the request / submission call. |

The wrap is a thin `with span_factory.flow_span("execute", org, project, slug, version) as span:` around the existing body; the no-op factory makes this a plain pass-through when off. Both `Flow` and `AsyncFlow` get it — Python via a `functools.wraps` decorator that reads `execution_id` / `status` off the return value (so replay / paused / auto-resumed / normal are all covered by one path); TS by extracting each body into a private `*Impl` wrapped by `spanFactory.flowSpan`.

### 3) The rename (hard)

Pure symbol/file surgery — no behavior change:

- **Python:** `git mv _trace_scope.py _replay_scope.py`; inside, `trace`→`replay` (both `@overload`s + impl), `trace_scope`→`replay_scope`, `trace_scope_sync`→`replay_scope_sync`; keep `current_session_id`, `_current_scope`, `_scope_var`; update the module docstring's "trace scope" wording and `__all__`. Update importers: `_flow.py:33`, `_step_iterator.py` (6 lazy imports), `__init__.py` (import line 74 + `__all__` 140-143 + the `# Replay / trace` comment 11), and the `_transport.py:52` docstring mention. Rename tests `tests/unit/test_replay*.py` references.
- **TypeScript:** in `replay/scope.ts` rename exports `traceScope`→`replayScope`, `TraceScopeOptions`→`ReplayScopeOptions` (file path stays); keep `currentSessionId`, `currentScope`, `scopeStorage`. Update `index.ts:86`, `adapters/express.ts:49,127`, `adapters/nextjs.ts:48,98`, `transport.ts` docstrings, and `tests/replay.test.ts`.

The `trace: bool` execute param, `run.trace()`, and `_models/trace.py`/`types/trace.ts` are untouched.

---

## Deliverables (PR plan) — each lands in Python **and** TypeScript

Ship order **PR-A → PR-B**. PR-A is independently valuable (removes the naming foot-gun). Two logical PRs = four physical PRs (one per repo each), landed in pairs. This design doc is committed first as a design PR.

### PR-A — Rename `trace*` → `replay*` (+ version re-sync)

| | Python | TypeScript |
|---|---|---|
| File move | `_trace_scope.py` → `_replay_scope.py` | none (rename exports in `replay/scope.ts`) |
| Renames | `trace`→`replay`, `trace_scope`→`replay_scope`, `trace_scope_sync`→`replay_scope_sync` | `traceScope`→`replayScope`, `TraceScopeOptions`→`ReplayScopeOptions` |
| Keep | `current_session_id`, `_current_scope`, `_scope_var` | `currentSessionId`, `currentScope`, `scopeStorage` |
| Importers | `_flow.py`, `_step_iterator.py`, `__init__.py`, `_transport.py` (docstring) | `index.ts`, `adapters/express.ts`, `adapters/nextjs.ts`, `transport.ts` (docstring) |
| Tests | rename usages in `tests/unit/test_replay*.py` (+ any `test_parity`/`test_interfaces`) | rename usages in `tests/replay.test.ts` (+ `interfaces.test.ts`, `types.test.ts`) |
| Docs | README "Replay scope" section; CHANGELOG `## [0.5.0]` **Changed (BREAKING)** + migration snippet | README replay section; CHANGELOG `## [0.5.0]` **Changed (BREAKING)** + migration snippet |
| Version | `pyproject.toml` `0.4.0`→`0.5.0`, `_version.py` | `package.json` `0.4.1`→`0.5.0`, `version.ts` |

**Migration note (both READMEs + CHANGELOG):**
```
# before → after
from noukai_sdk import trace, trace_scope, trace_scope_sync   →   replay, replay_scope, replay_scope_sync
import { traceScope, type TraceScopeOptions }                 →   replayScope, type ReplayScopeOptions
```
`current_session_id` / `currentSessionId` / `currentScope` are unchanged.

### PR-B — Customer-side OpenTelemetry (opt-in parent span)

| | Python | TypeScript |
|---|---|---|
| New file | `src/noukai_sdk/_otel.py` (no-op + guarded real factory) | `src/otel.ts` (mirror) |
| Packaging | `[project.optional-dependencies]` `otel = ["opentelemetry-api>=1.20"]` | `@opentelemetry/api` as optional `peerDependencies` + `peerDependenciesMeta` |
| Client | `Noukai`/`AsyncNoukai` gain `otel: bool = False`, `tracer=None`; build `_span_factory`, pass to transport | `NoukaiOptions` gains `otel?: boolean`, `tracer?`; same |
| Transport | store `_span_factory` (mirrors `_default_session_id`) | store `spanFactory` |
| Flow | decorate `execute` / `execute_async` with the span (sync + async) | extract each body into a private `*Impl` and wrap it with `flowSpan` |
| Errors | clear `NoukaiError` when `otel=True` but the extra is missing | `NoukaiError` thrown on the first traced call when the peer dep is absent |
| Docs | README `## OpenTelemetry` H2 with a runnable Jaeger/OTLP example; CHANGELOG **Added** | mirror |
| Tests | span emitted on/off, attributes, error status, no-import-when-off, missing-extra error | mirror with a vitest in-memory span exporter / fake tracer |

**Deferred (PR-C, documented, not built):** per-step child spans from `run.trace()` (backdated `start_time`/`end_time` from `StepTrace.started_at`/`completed_at`; attributes `gen_ai.request.model`=`model_used`, `gen_ai.usage.input_tokens`/`output_tokens`=`tokens.prompt`/`completion`, `noukai.step.cost_usd`=`cost_usd`, `noukai.step.id`/`status`); `traceparent` injection via `extra_headers` behind a `propagate_context=True` flag; **and spans on the streaming `steps()` / `events()` calls** (their caller-driven-iterator span lifetime shares the per-step machinery — shipping a fragile streaming span in PR-B was rejected in favour of deferring it here).

---

## Test plan

Per repo, all existing suites stay green plus:

**PR-A (rename):**
- The renamed public names are importable/exported; the old names are **gone** (a test asserting `ImportError`/absence guards against an accidental alias).
- `tests/unit/test_parity.py` (Py) still passes — sync/async twins intact.
- Interface/type tests updated to the new names.
- Gates — Python: `uv run ruff format --check` · `uv run ruff check` · `uv run mypy` · `uv run pytest tests/unit/ --cov=noukai_sdk --cov-fail-under=85`. TS: `pnpm typecheck` · `pnpm typecheck:examples` · `pnpm lint` · `pnpm test`. Cross-repo: `python scripts/check_parity.py` green (both `0.5.0`).

**PR-B (OTel):**
- **Off by default:** the no-op factory is used (asserted via `isinstance`/`instanceof`), and `execute` / `execute_async` behave identically to today, emitting no span.
- **On:** with an in-memory span exporter (Py `InMemorySpanExporter`; TS `@opentelemetry/sdk-trace-base` in-memory exporter), one CLIENT span per call with the expected name + attributes; `noukai.execution_id` / `status` set on completion.
- **Error path:** an execute that raises records the exception **exactly once** and sets span status ERROR, and the exception still propagates unchanged.
- **Missing extra/peer dep:** `otel=True` without the dependency raises the crisp install-hint `NoukaiError` (Python at construction; TS on the first traced call).
- **`tracer=` override:** spans go to the injected tracer, global provider not consulted.
- Sync + async both covered; parity test still green.

---

## Risks & mitigations

- **R1 — OTel-off overhead.** *Mitigation:* the no-op factory is a null context manager and the guarded import lives inside the "on" branch; a test asserts `opentelemetry` is never imported when off.
- **R2 — Breaking rename lands on a real user.** *Mitigation:* Alpha status + tiny replay-scope surface; migration snippet in README + CHANGELOG; the breaking change is isolated to PR-A and signalled by the minor bump.
- **R3 — Parity drift / half-landed change.** *Mitigation:* `check_parity.py` gate + the dual-land rule; each logical PR is reviewed as a pair and not merged until both repos are green on the same version.
- **R4 — streaming-span lifetime (why `steps` / `events` are deferred).** Correctly bounding a span across a caller-driven lazy iterator (open on first pull; close on exhaustion / early-break / error) without disturbing SSE-stream teardown is materially harder than a single-shot call and shares the per-step machinery. *Mitigation:* v1 defers streaming spans to PR-C rather than ship a fragile one; only the request-based `execute` / `execute_async` entry points are wrapped.
- **R5 — Semantic-convention churn.** OTel `gen_ai.*` conventions are still evolving. *Mitigation:* v1 parent span uses stable, self-namespaced `noukai.*` attributes only; `gen_ai.*` is confined to the deferred per-step spans where it belongs.

---

## Decisions made

1. **Hard rename, no deprecation alias.** Alpha SDKs, small surface; an alias shim would add code + tests for a name nobody should keep. One clean break at `0.5.0`.
2. **OTel v1 = parent CLIENT span on `execute` / `execute_async` only.** Highest value (end-to-end latency + status in the caller's stack), lowest risk, no extra network. Per-step spans, `traceparent`, and spans on the streaming `steps()` / `events()` calls are designed but deferred to PR-C.
3. **Opt-in via `otel=True` client flag**, acquiring a named tracer from the caller's global provider; `tracer=` overrides. Default off ⇒ never imports OTel. Chosen over "auto-detect a global provider" (weaker consent, harder to keep a true no-op) and over "always require an explicit tracer" (more verbose at the call site).
4. **Both repos → `0.5.0`**, re-syncing the current `0.4.0`/`0.4.1` drift and signalling the breaking rename per pre-1.0 convention.
5. **This is customer-side OTel only** — it does not reverse the server-side "no traces" decision (`20260706-BE-grafana-observability`); it is the v1.x client follow-through the original SDK briefs anticipated.
6. **`trace: bool` execute param left as-is** (server wire-field name); a future rename to `capture=` is flagged, not done here.
7. **Two accepted Python↔TS divergences.** (a) *Dependency resolution:* Python fails fast at client construction (`otel=True` without the extra raises immediately — a sync import); TS resolves `@opentelemetry/api` lazily on the first traced call (ESM dynamic import is async), so a missing peer dep surfaces there. (b) *Client-side validation errors:* Python decorates the whole `execute` method, so an invalid-argument call (both `message`+`messages`, `version="production"`, an async handler on the sync client) produces an ERROR `noukai.flow.execute` span; TS runs the same validators *before* opening the span, so it emits none. Both are error paths in the caller's own trust domain and do not change the span shape for real calls.

---

## References

- In-repo precedent (format + dual-land contract): `docs/design-logs/2026/09Sep/20260903-SDK-agent-relay.md` (both repos).
- Prior replay-scope design (code references it): `20260605-SDK-replay-decorator`.
- Original SDK briefs (OTel deferral): `.../design-logs/2026/05May/20260531-SDK-python-sdk-v1/design.md`, `.../06Jun/20260601-SDK-node-sdk-v1/design.md`.
- Server-side observability scope (no traces): `.../07Jul/20260706-BE-grafana-observability/`.
- Parity gate: `noukai-python-sdk/scripts/check_parity.py`.
- Wiring points (verified): `_flow.py`, `flow.ts`, `_client.py`, `client.ts`, `_run.py`, `run.ts`, `_models/trace.py`, `types/trace.ts`, `_transport.py`, `transport.ts`, `_trace_scope.py`, `replay/scope.ts`.
