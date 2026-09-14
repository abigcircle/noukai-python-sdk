# 20260903-SDK-agent-relay — Agent-over-relay in the Noukai SDK

- **Status:** Approved for build. Agent-home decision settled → **Option B (standalone `@noukai/agent` package)**. **PR-3 TS relocation is DONE** — `@noukai/agent` now lives in the `noukai-sdk` repo as its own releasable package (dir `noukai-agent-sdk`, npm `@noukai/agent`, v0.1.0), migrated out of the monorepo's `development/sdk/agent`. PR-1, PR-2, and the PR-3 rewire/Python-peer remain to build.
- **Date:** 2026-09-03
- **Author:** kii (with Claude)
- **Scope:** ships to **BOTH** SDKs together — `noukai-python-sdk` (`noukai-sdk`) **and** `noukai-typescript-sdk` (`@noukai/sdk`) — plus a consolidation of the in-repo `@noukai/agent` package. Every PR lands in both (see **Dual-land requirement** below).
- **Design-doc convention:** referenced from code as `20260903-SDK-agent-relay` (matches `20260605-SDK-replay-decorator`, `20260605-BE-execution-session-grouping`).

> **Repo note:** the parent `noukai-sdk/` is not itself a git repo — the two SDKs are. This doc lives at the parent so it can speak for both. When we act on it, copy it into whichever SDK repo lands the first PR (or add a `docs/design-logs/` to each and cross-link) so it's version-controlled with the code.

---

## TL;DR

Nouko is the prototype that proves out Noukai. Building the Pack Maker assistant surfaced a real gap: **the SDK's tool-calling agent loop cannot run across a trust boundary.** It assumes one process holds the `nk_` key, drives the loop, *and* executes the tools. But every browser-facing agent splits that: the browser drives the loop and executes tools; a keyholder server relays the round-trips.

Because the SDK couldn't do this, we grew a **second** loop implementation (`@noukai/agent`) and **hand-rolled** the server relay in each app (`nouko-pack-agent`, and noukai's Nana BFF). We now maintain the same yield/resume algorithm twice, in two wire-shape copies, with two different round limits — and a keyholder proxy per product. The SDK's request model has even **drifted behind the server** (it lacks the `messages` field the new agent block consumes).

This design makes the loop **transport-pluggable** and adds a **relay adapter**, so the loop is written *once* and runs in three positions (direct / server-relay / browser) against one wire contract. It folds `@noukai/agent` into the SDK family as an agent slice. It is designed to **plug into the current nouko + noukai code** — `nouko-pack-agent` collapses to "mount the adapter + an authorize hook," and the FE keeps `useAgentChat` while its engine moves onto the SDK. **Every deliverable lands in both the Python and TypeScript SDKs in lockstep.**

---

## Dual-land requirement & parity contract

This is a **two-SDK feature by construction, not by aspiration.** The whole point is that Noukai consumers — Python servers *and* TypeScript/browser apps — get the same capability. So:

1. **Every PR ships Python + TypeScript together**, in the same change set, and is not considered done until both are implemented, tested, README-documented, and example-covered. No "TS-first, Python later" — they land as a pair.
2. **Same released `CHANGELOG.md` version in both repos.** `noukai-python-sdk/scripts/check_parity.py` compares the top released `## [X.Y.Z]` heading across the two repos and fails CI on drift. The paired bump is part of the PR.
3. **Async/sync symbol parity holds** within Python (`tests/unit/test_parity.py`: any public method on `AsyncNoukai/AsyncFlow/AsyncRun` has a sync twin, modulo `aclose`/`close`).
4. **Structural mirror across languages.** The two SDKs are already file-for-file mirrors (`_transport.py`↔`transport.ts`, `_tool_calls.py`↔`tool-calls.ts`, `_paths.py`↔`paths.ts`, `adapters/*`). New files mirror: `adapters/relay.py`+`relay` blueprint ↔ `adapters/express.ts`/`nextjs.ts` relay handlers; the execute-transport seam mirrors both sides.
5. **One divergence is legitimate and only one:** the **React hook** (`useAgentChat`) is TS-only because React is. Its *engine* — the loop, the relay transport, the relay-flow entrypoint, the request/response models — lands in **both** SDKs. Python's agent slice is the framework-agnostic loop (no UI binding), which is exactly what a Python server or CLI relay consumer needs.

Read every PR below as "do this in Python **and** TypeScript."

---

## Background: what exists today

### The three homes for "agent execution"

```
                         ┌───────────────────────────────────────────────┐
                         │  noukai router-ai-slugs                        │
                         │  POST /seq/{org}/{project}/{slug}/execute      │
                         │  SeqflowExecuteRequest: message | messages     │
                         │  (yield/resume tool-calling; nk_ bearer)       │
                         └───────────────▲───────────────▲───────────────┘
             key-holding, direct         │               │   key-holding, verbatim relay
        ┌────────────────────────────────┘               └────────────────────────────┐
        │                                                                               │
┌───────┴────────────────────────────┐                          ┌─────────────────────┴───────────────┐
│ (1) noukai-sdk / @noukai/sdk        │                          │ (3) HAND-ROLLED SERVER RELAY         │
│  flow.execute(tool_handler=…)       │                          │  nouko-pack-agent (Python, httpx)    │
│  autoResumeLoop + PausedResult      │                          │  noukai Nana BFF (per-app)           │
│  ToolCallLimitError (limit 10)      │                          │  injects nk_, pins org/proj/slug,    │
│  paths.py = /seq SoT                │                          │  gates role, bounds body/messages    │
│  transport THROWS on non-2xx        │                          │  = the missing SDK piece             │
│  ExecuteRequest LACKS `messages`    │                          └─────────────────────▲───────────────┘
└─────────────────────────────────────┘                                                │ keyless POST {messages|toolCallMessages}
        ▲  (the SDK a Python server would call)                     ┌──────────────────┴──────────────────┐
        │  SECOND copy of the loop ─────────────────────────────▶  │ (2) @noukai/agent  (TS, v0.0.0)      │
        │                                                          │  runAgentLoop(endpoint, …)           │
        │                                                          │  re-declares PausedResponse/…        │
        │                                                          │  maxIterations (limit 12)            │
        │                                                          │  hand-builds {messages,tools,…} body │
        │                                                          │  useAgentChat (React) — browser      │
        │                                                          │  consumed by BOTH Nana + Pack Maker  │
        │                                                          └──────────────────────────────────────┘
```

**(1) The SDK loop — direct, key-holding.**
- `Flow.execute(..., tool_handler=…)` auto-resumes; `tool_handler=None` returns a typed `PausedResult` the caller drives via `.resume()` (async only). — `_flow.py:152`, `flow.ts:115`.
- Loop + client round limit: `_auto_resume_loop`/`autoResumeLoop`, `ToolCallLimitError`. — `_tool_calls.py:179`, `tool-calls.ts:157`. Limit 10 both.
- Resume is a **closure welded to the direct transport** — captures `flow._transport` + `f"{flow._versioned_path(version)}/execute"`. — `_tool_calls.py:93-94`, `tool-calls.ts:96-101`.
- Wire single-source-of-truth: `_paths.py`/`paths.ts` (`flow_execute_path`/`flowExecutePath`).
- Transport injects the `nk_` bearer, retries, and **throws** on non-2xx. — `_transport.py:225`, `transport.ts:429`.
- **`ExecuteRequest` has `message` but no `messages`** (`_models/requests.py:13`, `types/requests.ts`) even though its docstring says it "mirrors server SeqflowExecuteRequest." It has drifted behind the server (see the server contract below).
- Adapters today (`adapters/fastapi.py`, `flask.py`, `express.ts`, `nextjs.ts`) are **trace/replay middleware only** — not relays.

**(2) `@noukai/agent` — the browser/relay loop (`development/sdk/agent/`).**
- Self-described "single source of truth for the agent engine… will move to a versioned npm publish when mature," shared by noukai + nouko via `link:`.
- `runAgentLoop(message, options)` POSTs to a caller-provided `endpoint` (the relay), **no key**, drives the **same** `/execute` yield/resume shape, and **hand-builds** the fresh body (`{messages, tools, toolChoice}` or `{message, tools, parameters}`) — precisely because the SDK request model can't express `messages`. Round limit `DEFAULT_MAX_ITERATIONS = 12`.
- **Re-declares** `PausedResponse`/`CompletedResponse` (`agent-loop.ts:65-84`) — duplicates of the SDK's `PausedResult`/`ExecuteResult`.
- `useAgentChat` is a thin React wrapper. `protocol.ts` (opaque `stateToken`) is **aspirational/unused, no importers**.

**(3) The server relay — hand-rolled, per app.** Nouko `nouko-pack-agent` (maker-gated, bounded httpx passthrough) and noukai's Nana BFF each re-implement "inject key, pin coords, forward `/execute`, relay status verbatim, bound abuse." **None of it is in the SDK.**

### The server contract — router-ai-slugs (authoritative)

The agent block the user recently landed is live in `SeqflowExecuteRequest` — `development/noukai/services/executor/router-ai-slugs/src/router_ai_slugs/models/seqflow.py:40` and the fresh/resume detection in `services/lane_executor.py`:

- **Fresh call:** `message: str | None` **or** `messages: list[ChatMessage] | None` — either satisfies a fresh call (`lane_executor.py:98`). `messages` is "structured prior conversation for chat flows; the last entry is the current user turn… fed to the model as real role:user/assistant turns (not flattened)." When set, it **stands in for `message`** (`seqflow.py:49-57`).
- **`messages[]` roles are restricted:** `user` / `assistant` / `tool` only. A `system` or `function` turn → **400 `MESSAGES_ROLE_INVALID`** (`lane_executor.py:105-116`) — a caller system turn would override the flow author's rendered system prompt (v0 decision 4).
- **Resume** is detected by `executionId` **and** `pausedAtStep` **and** `toolCallMessages` all present (`is_resume_request`, `lane_executor.py:71`).
- **Size cap:** both `messages[]` and `toolCallMessages` are capped at `MAX_MESSAGES_PAYLOAD_BYTES` (1 MB) → **413 `MESSAGES_TOO_LARGE`** (`validate_messages_payload_size`, `lane_executor.py:155`).
- `ChatMessage`, `ToolDef`, `ToolChoice` come from the shared `llm_service.models`; `Attachment` from `noutai_content_types`.
- **Responses:** `SeqflowExecuteResponse` (`status: completed|failed`, `executionId`, `result`, `flowId`, `blockCount`) and `SeqflowExecutePausedResponse` (HTTP 200: `status:"tool_calls_required"`, `executionId`, `pausedAtStep`, `iterationsUsed`, `toolCallMessages`, `toolCalls`, `accumulatedOutputs`, `flowId`, `blockCount`). Tool error codes incl. `TOOLS_NOT_ENABLED`, `TOOL_ITERATION_LIMIT`, `MESSAGES_TOO_LARGE`, `TOOLS_REQUIRE_SYNC_EXECUTE` (`seqflow.py:354`).

The SDK's `PausedResult`/`ExecuteResult` already match the two responses. **The request side does not** — it's missing `messages`.

---

## The problem, precisely (findings)

- **F1 — The tool-call loop is implemented twice.** `@noukai/sdk` `autoResumeLoop` (direct) and `@noukai/agent` `runAgentLoop` (relay) are the same algorithm against the same wire shape, with duplicated response types and different round limits (10 vs 12).
- **F2 — The SDK loop is welded to the direct, key-holding transport.** No seam to point it at a keyless relay endpoint — which is *why* `@noukai/agent` exists separately.
- **F3 — There is no server relay in the SDK.** The keyholder proxy is re-hand-rolled per product. The transport already has key injection + `/seq` path; it lacks only a **non-raising mode** (it throws on non-2xx; a verbatim relay must pass 4xx/5xx through).
- **F4 — `@noukai/agent` is outside the versioned SDK family.** TS-only, `v0.0.0`, `private`, own wire copies, no Python peer.
- **F5 — The relay echoes raw server execution-state through the untrusted browser** (`executionId`, `pausedAtStep`, `iterationsUsed`, `toolCallMessages`, `accumulatedOutputs`). The unused `protocol.ts` sketches the fix (opaque signed `stateToken`); hardening it is a coordinated BE follow-on, not this design.
- **F6 — The SDK request model has drifted behind the server.** `SeqflowExecuteRequest` grew `messages` (the agent block); the SDK's `ExecuteRequest` (`_models/requests.py:13`, `types/requests.ts`) still only has `message`. A Python server therefore **cannot** call a chat/agent flow through the SDK at all, and the browser loop must hand-build the body. This is the direct blocker to using the SDK for the exact use case we're building.

---

## Goals / Non-goals

**Goals**
1. Write the yield/resume loop **once** and run it in three positions: direct (server holds key), server-relay (keyholder proxy), browser (keyless, drives loop + executes tools).
2. Add a **relay adapter** to both SDKs so `nouko-pack-agent` and the Nana BFF become thin mounts.
3. Bring the SDK request model back in sync with the server — add **`messages`** (F6) so chat/agent flows are first-class in both SDKs.
4. Fold `@noukai/agent` into the SDK family as an **agent slice** (subpath) so browsers import only the loop + tools + React hook.
5. **Plug-and-play** into current nouko + noukai code with minimal churn and no behavior change at first.
6. **Land every change in both SDKs in lockstep** (see Dual-land requirement).

**Non-goals (explicit)**
- No `proxy=True` flag on `flow.execute()` — relay is a transport concern.
- The relay does **not** deserialize/re-serialize business payloads — verbatim forward.
- App authorization (nouko's maker-role) and bound **values** never move into the SDK — hooks/config only.
- Not shipping the `stateToken` hardening (F5) here — flagged, deferred, needs BE.
- Not breaking `@noukai/agent` — it becomes a re-export shim during migration.

---

## Design overview

One idea unlocks everything: **make the loop's "send an execute/resume request" step pluggable.**

```
              ┌──────────────────────────────────────────────┐
              │  Agent loop (ONE implementation, per SDK)     │
              │  - fresh call (message | messages)            │
              │  - on tool_calls_required: resolve → resume   │
              │  - client round limit → ToolCallLimitError    │
              │  - typed PausedResult / ExecuteResult         │
              └───────────────────────┬──────────────────────┘
                                      │ uses
                        ┌─────────────▼─────────────┐
                        │  ExecuteTransport (seam)   │
                        │  send(payload)->(status,body)
                        └─────┬───────────────┬──────┘
        ┌────────────────────▼──┐        ┌────▼───────────────────────┐
        │ DirectExecuteTransport │        │ RelayExecuteTransport       │
        │ has nk_ key, builds     │        │ no key, POSTs raw body to   │
        │ /seq path → noukai      │        │ a caller-given relay URL    │
        │ (today's behavior)      │        │ (browser / server-to-server)│
        └─────────────────────────┘        └─────────────────────────────┘
```

- The loop, models, and round limit already exist and are transport-agnostic except for the welded closure. We cut the weld and route the send through the seam. `flow.execute()` keeps `DirectExecuteTransport` — **zero behavior change**.
- The **relay adapter** (server) receives a payload, runs it through a `DirectExecuteTransport` in **non-raising** mode, and relays the result verbatim — after an app `authorize` hook + bound config.
- The **browser (and any keyless consumer)** uses `RelayExecuteTransport` pointed at the relay URL and drives the same loop.

One wire contract, three positions, one loop — mirrored in both SDKs.

---

## Deliverables (PR plan) — each lands in Python **and** TypeScript

Ship order PR-1 → PR-2 → PR-3; PR-1 delivers value alone. Each PR's Definition of Done is at the end.

### PR-1 — Verbatim relay transport mode + relay adapter (the server win)

**Non-raising transport (both SDKs, backward compatible):**

| | Python | TypeScript |
|---|---|---|
| Change | add `raise_for_status: bool = True` to `AsyncTransport.request` **and** `SyncTransport.request` | add `raiseForStatus?: boolean` (default `true`) to `RequestOptions` |
| When `False` | return `Response` even on non-2xx (skip `_map_status_to_exception`) | return `TransportResponse` even on non-2xx (skip `mapStatusToError`) |
| File | `_transport.py:125,370` | `transport.ts:326` |

**Relay adapter — `adapters/relay`, mirroring the existing adapter matrix:**

| SDK | Framework | Proposed surface |
|---|---|---|
| Python | FastAPI | `mount_flow_relay(app, *, client, org, project, slug, path="/agent/execute", authorize, max_body_bytes=262144, max_messages=40, version="draft")` |
| Python | Flask | `flow_relay_blueprint(*, client, org, project, slug, authorize, bounds=…)` |
| TS | Express | `noukaiRelayHandler({ client, org, project, slug, authorize, bounds })` → express handler |
| TS | Next.js | `createRelayRoute({ client, org, project, slug, authorize, bounds })` → Route Handler |

Adapter responsibilities (only these): (1) read raw body, bound `max_body_bytes` on raw bytes **before** parse, bound `messages`/`toolCallMessages` counts by `max_messages`; (2) `await authorize(request)` (nouko passes its maker-role check); (3) forward verbatim to `flow_execute_path(...)` via the transport with `raise_for_status=False`, injecting the `nk_` bearer; (4) relay upstream `(status, body)` **verbatim** — non-JSON upstream → generic `{"detail":"UPSTREAM_NON_JSON"}` at the upstream status; (5) never log key/body. Framework imports stay **lazy** (mirror `adapters/fastapi.py`'s lazy Starlette import; TS structural types + optional peer deps).

**Exports:** subpath only — `from noukai_sdk.adapters.relay import mount_flow_relay`; `import { noukaiRelayHandler } from "@noukai/sdk/adapters/express"`.

**README (both):** new H2 `## Serving a flow to a browser (relay)` with a runnable example.

**Plug-and-play:** `nouko-pack-agent` swaps `GatewayService` for `mount_flow_relay(...)`, passes `require_maker_role` as `authorize`, moves `256 KiB/40` into `bounds`; the FE contract is unchanged. Nana BFF: same swap.

---

### PR-2 — Transport-pluggable loop + `messages` support (the unification)

**(a) Execute-transport seam — both SDKs, non-breaking:**
- Python: a `Protocol` `ExecuteTransport` with `async def send(payload) -> tuple[int, dict]` (+ sync mirror). `DirectExecuteTransport(flow)` wraps `flow._transport.request` + `_versioned_path`. `_attach_resume`/`_auto_resume_loop` take an `ExecuteTransport` instead of reaching into `flow`. — `_tool_calls.py`, `_flow.py`.
- TS: `interface ExecuteTransport { send(payload): Promise<{status; body}> }`; `DirectExecuteTransport` wraps `flow._transport` + `flowExecutePath`; `attachResume`/`autoResumeLoop` take it. — `tool-calls.ts`, `flow.ts`.
- `flow.execute()` builds a `DirectExecuteTransport` and passes it in — byte-for-byte behavior preserved (existing tool-call tests are the regression guard).

**(b) Close F6 — add `messages` to the request model, both SDKs:**
- Add `messages: list[ChatMessage] | None` to `ExecuteRequest` (`_models/requests.py`) and `types/requests.ts`, with a `ChatMessage` type (role `user|assistant|tool`, content, tool_calls/tool_call_id) mirroring `llm_service.models.ChatMessage`.
- Add a `messages=` path to `flow.execute()` (both SDKs): either `message` or `messages` on a fresh call; **client-side validate** the server's constraints so failures surface early — reject `system`/`function` roles, require exactly one of `message`/`messages`, and warn at the 1 MB soft limit (extend the existing `_check_tool_messages_size`).
- Reconcile the two client round limits to **one** constant (choose 10 or 12 deliberately; call it out in `CHANGELOG`).

**(c) Keyless relay entrypoint — both SDKs:**
- `RelayExecuteTransport({ url, fetch? })` (TS) / `RelayExecuteTransport(url, *, client=None)` (Python) — POSTs the raw payload to `url`, no key, no `/seq`, returns `{status, body}`.
- `createRelayFlow({ url, fetch? })` (TS) / `RelayFlow(url)` (Python) — `.execute({ message | messages, tools, toolChoice, toolHandler, maxToolRounds, signal })` runs the shared loop over the relay transport, returns `ExecuteResult | PausedResult`.
- Both fresh-call modes supported (single `message` and structured `messages`).

**README (both):** extend `## Tool calls` with `### Driving the loop through a relay` + example.

**Plug-and-play:** `@noukai/agent`'s `runAgentLoop` is re-expressed as `createRelayFlow(...).execute(...)` — same endpoint, same wire, same modes — so `useAgentChat` keeps its shape while its engine moves onto the SDK; the duplicate `PausedResponse`/`CompletedResponse` in `agent-loop.ts` are deleted.

---

### PR-3 — The agent framework as its own package (Option B) + consolidation (both SDKs)

**Decision (settled): Option B — a standalone `@noukai/agent` package that depends on `@noukai/sdk`**, not a `@noukai/sdk/agent` subpath. Rationale: the agent framework *consumes* the SDK (loop + models) rather than being part of it; keeps React out of the base SDK; independent version cadence; honors the extraction's stated "own npm publish" intent. Package boundary ≠ download boundary — browser consumers tree-shake either way, so nothing is lost on size.

- **TS — relocation DONE.** `@noukai/agent` has been migrated out of the monorepo (`development/sdk/agent`) into the `noukai-sdk` repo as a standalone, releasable package (`noukai-agent-sdk` dir, npm `@noukai/agent` v0.1.0, tsup build, own CHANGELOG/RELEASING). **Remaining TS work (after PR-2):** make `@noukai/agent` *depend on* `@noukai/sdk` — delete its re-declared `PausedResponse`/`CompletedResponse` and hand-rolled loop, re-express `runAgentLoop` over PR-2's `createRelayFlow`/`RelayExecuteTransport` (this is the F1 kill). Then publish, migrate both web apps from `link:` (raw source) to the published semver, and remove the monorepo copy.
- **Python — the peer as its own package (mirrors Option B).** Ship `noukai-agent` on PyPI (depends on `noukai-sdk`) — the framework-agnostic loop (`runAgentLoop` equivalent) over `RelayExecuteTransport`, `ToolRegistry`, tool wire-adapters. No React binding (the one legitimate cross-language gap). **Gated on a real Python consumer** (server-to-server relay / CLI agent); design the surface now, build when consumed. Do NOT add it to `check_parity.py` (that gate pairs only the two existing SDKs).
- **F5 (deferred):** adopt the `protocol.ts` opaque `stateToken` so the browser stops seeing raw server execution-state — **requires router-ai-slugs** to mint/verify the token; a separate BE-coordinated design. Documented as the north-star hardening.

**README:** `@noukai/agent`'s own README documents the framework + a full React `useAgentChat` example; the Python `noukai-agent` README documents the loop primitive.

**Plug-and-play:** nouko `use-pack-maker-agent.ts` and noukai `use-ai-pipeline-chat.ts` keep importing `@noukai/agent`; only the resolution flips from `link:` (raw monorepo source) to the published package. Public hooks untouched.

---

## Wire contract (appendix — authoritative, from router-ai-slugs)

Source: `SeqflowExecuteRequest`/`SeqflowExecuteResponse`/`SeqflowExecutePausedResponse` in `models/seqflow.py`; fresh/resume rules in `services/lane_executor.py`.

**Fresh call — structured/chat (Pack Maker, agent block):**
```json
POST <relay-url>   (relay forwards verbatim to /seq/{org}/{project}/{slug}/execute)
{ "messages": [ {"role":"user","content":"…"} ], "tools": [ /* ToolDef */ ], "toolChoice": "auto" }
```
- `messages[]`: `role ∈ {user, assistant, tool}` only (`system`/`function` → 400 `MESSAGES_ROLE_INVALID`); last entry = current user turn.

**Fresh call — single message (Nana):** `{ "message": "…", "tools": [...], "parameters": { "conversation": [...] } }`

**Yield (200):** `{ "status":"tool_calls_required", "executionId", "pausedAtStep", "iterationsUsed", "toolCallMessages":[…], "toolCalls":[…], "accumulatedOutputs":{…}, "flowId", "blockCount" }`

**Resume:** `{ "executionId", "pausedAtStep", "toolCallMessages":[…prev, …toolResults], "iterationsUsed", "accumulatedOutputs", "tools" }` — all three of `executionId`/`pausedAtStep`/`toolCallMessages` required.

**Complete:** `{ "status":"completed", "result", "flowId", "blockCount" }`

**Caps/errors:** `messages[]` and `toolCallMessages` ≤ 1 MB → 413 `MESSAGES_TOO_LARGE`; tool errors incl. `TOOLS_NOT_ENABLED`, `TOOL_ITERATION_LIMIT`, `TOOLS_REQUIRE_SYNC_EXECUTE`. The relay forwards all of the above **verbatim**. **F5 target:** replace the echoed execution-state fields with one opaque `stateToken`.

---

## Critical analysis

### Rejected alternatives
- **`flow.execute(proxy=True)` / raw-mode flag on the high-level client** — overloads execute with a transport concern and drags error interpretation into a verbatim path.
- **Relay that deserializes then re-serializes** — lossy; couples the relay to model versions; breaks on server field additions. Verbatim byte pipe only.
- **Ship the full SDK into the browser / keyholder relay** — dep + supply-chain surface. Hence `@noukai/agent` depends only on `@noukai/sdk` (tree-shakeable) + lazy framework imports; relay adapters keep frameworks as optional peers.
- **`@noukai/sdk/agent` subpath instead of a standalone package** — considered and rejected (see PR-3 decision): couples release cadence, drags React peer into the base SDK, and buys no size win (package boundary ≠ download boundary). Standalone `@noukai/agent` depending on `@noukai/sdk` is the chosen boundary.
- **Keep `@noukai/agent` re-declaring its own wire types forever** — guarantees the two-loops divergence (F1). It must depend on `@noukai/sdk`'s models (PR-3 rewire).
- **Let the request model keep drifting** (F6) — a Python server literally can't call an agent flow. Sync it.

### What must NOT leak into the SDK
- App authorization (maker-role) → `authorize` hook only.
- Bound **values** (256 KiB / 40) → adapter config, never SDK constants.
- Business payload interpretation → never.
- The relay must not raise typed errors in place of relaying status.

### Definition of done (per PR — enforced)
- **Landed in BOTH SDKs** in the same change set (this is the gate, not a nicety).
- Both `CHANGELOG.md` bumped to the **same** version (`check_parity.py` green).
- Async/sync symbol parity holds (`test_parity.py`).
- README updated **and** a runnable example added in **both** SDKs.
- No behavior change for existing `flow.execute()` callers.
- The relevant nouko/noukai call site compiles against the new surface.

---

## Risks & open questions
- **Round-limit reconciliation (10 vs 12).** Choosing one is a tiny behavior change for one side — call it out in the CHANGELOG.
- **F5 needs the backend.** The `stateToken` hardening can't ship SDK-only; separate BE-coordinated design. Until then, relays echo server execution-state (acceptable behind auth + bounds).
- **Version-bump coordination.** `check_parity.py` fails on released-version drift — land the paired bump in the same change.
- **Migration timing for `@noukai/agent`.** The relocation into the `noukai-sdk` repo is done; the apps still `link:` the (now-removed-once-verified) monorepo copy as raw source. The `link:` → published-semver flip waits for (a) the PR-3 rewire onto `@noukai/sdk` and (b) an actual `@noukai/agent` npm publish. Keep both apps green throughout.
- **`ChatMessage` shape fidelity.** The SDK's new `ChatMessage` must track `llm_service.models.ChatMessage` (role/content/tool_calls/tool_call_id/name). Keep it a thin permissive model and let the server validate — the SDK's job is to *express* `messages`, not re-police it (beyond the early role/either-required/size checks in PR-2b).

**Resolved (was open):** the chat `messages` contract is now confirmed against `router-ai-slugs` — `message | messages`, `messages[]` = user/assistant/tool only, resume via `executionId+pausedAtStep+toolCallMessages`, 1 MB cap. Encoded in PR-2b and the wire appendix.

---

## Rollout
0. **DONE** — Agent-home decision (Option B) + `@noukai/agent` relocated into the `noukai-sdk` repo as a standalone releasable package (still consumed by both apps via `link:` to the monorepo copy for now).
1. PR-1 → release `@noukai/sdk` + `noukai-sdk` (both SDKs, same version). Adopt in `nouko-pack-agent` + Nana BFF (thin mount). No FE change.
2. PR-2 → release (both). `messages` lands in the request model (F6); `ExecuteTransport` seam + `RelayExecuteTransport`/`createRelayFlow` ship.
3. PR-3 rewire → rework `@noukai/agent` to depend on `@noukai/sdk` (delete its duplicate wire types + loop, re-express over `createRelayFlow`), publish it, migrate both apps `link:` → semver, remove the monorepo copy. Build the Python `noukai-agent` peer when a Python consumer appears. Schedule F5 separately with BE.
