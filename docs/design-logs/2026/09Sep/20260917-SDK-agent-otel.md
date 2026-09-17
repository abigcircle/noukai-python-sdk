# 20260917-SDK-agent-otel — Client-side OpenTelemetry for the agent loop (unified browser↔relay trace)

- **Status:** Implemented (PR-1 landed in `@noukai/agent`; PR-2 relay forwarding is a paired base-SDK change — see below).
- **Date:** 2026-09-17
- **Author:** kii (with Claude)
- **Scope:** `@noukai/agent` (TS-only) for the loop spans + `traceparent` injection; a paired, parity-bound change in `@noukai/sdk` + `noukai-sdk` for relay `traceparent` forwarding.
- **Builds on:** `20260916-SDK-otel-and-replay-rename` (base SDK customer-side OTel), `20260903-SDK-agent-relay` (the relay architecture), `20260914-SDK-agent-background-turns` (the detached turn manager).

---

## TL;DR

The base SDK's OTel (`20260916`) traces `flow.execute` only — but the agent
architecture (browser → **verbatim relay** → Noukai) never calls `flow.execute`,
so nothing on that path emits spans, and no server-side trace can see the
**client-side tool executions** (`resolveToolCall` runs in the browser). This
design adds opt-in OTel to the loop itself and injects W3C context so the
browser trace unifies with an instrumented relay.

Two PRs:

1. **PR-1 (`@noukai/agent`, TS-only).** All client-side spans + `traceparent`
   injection. Self-contained; unifies browser↔relay for any relay running
   standard OTel HTTP instrumentation.
2. **PR-2 (base SDKs, dual-land Py+TS).** `noukaiRelayHandler` / `createRelayRoute`
   forward incoming `traceparent`/`tracestate` on the relay→Noukai hop, extending
   the trace to the Noukai ingress. Plus the PII-gated tool payloads in the agent
   SDK.

## What "unified" reaches

The Noukai backend has **no server-side OTel** (per `20260706-BE-grafana-observability`
and reaffirmed in `20260916`). The unified trace, in the customer's own backend, is:

```
browser: invoke_agent
  ├─ noukai.agent.round ─POST(traceparent)→ relay: HTTP server span   ← W3C join
  │                                            └─ relay→Noukai (traceparent forwarded, PR-2)
  │                                                  └─ [Noukai ingress — black box, trace ends]
  └─ execute_tool {name}   (local resolution)
```

The browser↔relay join is standard W3C context propagation: the browser injects,
the relay's own HTTP instrumentation extracts. **No base-SDK change is required
for that join** — only for forwarding the header the last hop to Noukai (PR-2).

## Span model (locked)

| Span name | Kind | Attributes |
|---|---|---|
| `invoke_agent` | INTERNAL | `gen_ai.operation.name=invoke_agent`, `noukai.agent.tools: string[]`, `noukai.agent.request_mode: message\|messages`, `noukai.agent.max_rounds`, `session.id?`; at end: `noukai.agent.rounds`, `noukai.agent.termination: completed\|max_iterations\|error` |
| `noukai.agent.round` | CLIENT | `noukai.agent.round_index`, `http.response.status_code`; status ERROR on ≥400 / throw |
| `execute_tool {name}` | INTERNAL | `gen_ai.operation.name=execute_tool`, `gen_ai.tool.name`, `gen_ai.tool.call.id`, `noukai.tool.cache_hit`; with `toolPayloads`: `noukai.tool.arguments`, `noukai.tool.result` (each bounded to 4096 chars) |

Follows OTel GenAI semantic conventions (`gen_ai.*`) where they exist; `noukai.*`
for the loop-specific bits.

## Design decisions

1. **Mirror the base SDK's factory shape, don't share it.** New `src/otel.ts` in
   `@noukai/agent` with the same no-op / lazy-import / real triplet as
   `@noukai/sdk`'s `otel.ts`. `@opentelemetry/api` is an **optional peer dep**,
   imported dynamically on the first traced loop; missing dep → a clear
   `NoukaiError`. Off path never imports OTel (verified by the no-op test).

2. **Explicit parenting — no `ContextManager` assumption.** Browsers usually have
   no registered context manager, so `startActiveSpan` wouldn't propagate. Each
   child is parented by passing an explicit `Context` built with
   `trace.setSpan(parent, span)` — same technique the base SDK uses.

3. **Round spans + injection via the existing `fetch` seam.** `runAgentLoop`
   already forwards a `fetch` to `createRelayFlow`. We wrap it: per call → one
   `noukai.agent.round` CLIENT span, `propagation.inject(roundContext, headers)`,
   then delegate. No base-SDK change, and injection uses the round span's context
   so a downstream relay nests under the correct span. Uses the app's global
   propagator (standard `provider.register()` sets W3C).

4. **Tool spans wrap `resolveToolCall` in `toolHandler`.** The dedup-cache serves
   are recorded as zero-work `execute_tool` spans with `cache_hit=true`; fresh
   resolutions wrap the resolver call and (with `toolPayloads`) attach bounded
   args/result.

5. **Termination reason at the three loop exits.** `completed` on a normal
   return, `max_iterations` on the `ToolCallLimitError`→sentinel and the
   defensive residual-pause path, `error` set by the turn span's own catch when
   anything (incl. a `status:"failed"` flow) throws out of the loop body.

6. **Background turns thread `otelContext` explicitly.** The turn manager runs a
   detached `void runTurn(...)` with no ambient context; the parent `Context` is
   snapshotted into `ExecutionSnapshot` at send, and `sessionId` labels the span.

## Non-goals / deferred

- **Per-pipeline-block child spans under a round.** Would require adding OTel to
  `createRelayFlow` in the base SDK (dual-land). The relay is verbatim; the
  block-level trace is already available server-side via `run.trace()` at the
  relay's Direct position. Not built.
- **Server-side OTel inside the flow.** Out of scope; the trace terminates at the
  Noukai ingress until server tracing exists.
- **A non-OTel callback seam.** Could sit under the factory later; not needed now.

## Parity note

`@noukai/agent` is **TS-only** and is **not** in the `check_parity.py` gate, so
PR-1 lands alone. PR-2 (relay `traceparent` forwarding) touches
`@noukai/sdk` + `noukai-sdk` and **is** parity-bound — it lands as a coordinated
pair at the same version, mirrored file-for-file (`adapters/relay.ts` ↔
`adapters/relay.py`).
