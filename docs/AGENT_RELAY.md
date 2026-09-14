# Agent-over-relay — implementation guide (`noukai-sdk`, Python)

> **Audience: an LLM or developer implementing a relay.** This is a dense,
> self-contained spec. Read the [mental model](#1-mental-model-three-positions)
> first, decide [which position you are in](#2-which-position-am-i-in), then copy
> the matching recipe. Every code block is grounded in the shipped API — do not
> invent options that are not listed here.
>
> Design of record: `20260903-SDK-agent-relay` (see
> [`docs/design-logs/2026/09Sep/20260903-SDK-agent-relay.md`](./design-logs/2026/09Sep/20260903-SDK-agent-relay.md)).
> TypeScript peer: [`@noukai/sdk` `docs/AGENT_RELAY.md`](../../noukai-typescript-sdk/docs/AGENT_RELAY.md).

---

## 1. Mental model: three positions

A Noukai flow can pause mid-run to ask the caller to execute tools (the
**yield/resume tool-calling loop**). The `nk_` API key authorizes the flow. The
problem a relay solves: **the code that drives the loop and executes the tools is
often not the code that holds the key.** A browser must never see `nk_`.

The loop is written **once** and runs in three positions. The *only* thing that
changes between them is the **transport** — how each `/execute` round-trip is
sent.

```
  ┌───────────────────────────────────────────────────────────────┐
  │  Noukai server:  POST /seq/{org}/{project}/{slug}/execute      │
  │  yield/resume tool-calling; requires the `nk_` bearer          │
  └───────────▲───────────────────────────────▲──────────────────┘
   key-holding │ (DirectExecuteTransport)      │ key-holding, verbatim relay
              │                                │  (RelayExecuteTransport target)
  ┌───────────┴───────────┐        ┌───────────┴───────────────────────────┐
  │ POSITION 1: DIRECT     │        │ POSITION 2: SERVER RELAY (keyholder)   │
  │ flow.execute(...)      │        │ mount_flow_relay / flow_relay_blueprint│
  │ same process holds key,│        │ holds nk_, bounds abuse, authorize(),  │
  │ drives loop, runs tools│        │ forwards VERBATIM, relays status/body  │
  └────────────────────────┘        └───────────▲────────────────────────────┘
                                                 │ keyless POST (no nk_)
                                    ┌────────────┴───────────────────────────┐
                                    │ POSITION 3: KEYLESS CLIENT              │
                                    │ RelayFlow(url).execute(...)             │
                                    │ server-to-server / CLI agent:           │
                                    │ drives the SAME loop, runs tools locally│
                                    └────────────▲───────────────────────────┘
                                                 │ browser/React front end
                                    ┌────────────┴───────────────────────────┐
                                    │ POSITION 3b: REACT AGENT — TS ONLY      │
                                    │ @noukai/agent useAgentChat (see §6)     │
                                    └─────────────────────────────────────────┘
```

- **Position 1 — Direct.** Your key-holding server calls `flow.execute()`. No
  relay involved. This is the baseline; documented in the [main README](../README.md#tool-calls).
- **Position 2 — Server relay.** A keyholder proxy you mount on your own
  FastAPI/Flask server. It is a thin, dumb, verbatim byte-pipe (with abuse bounds
  + an auth hook). This is what you build to let a browser (or another service)
  drive a flow.
- **Position 3 — Keyless client.** Any code with **no** `nk_` key that drives the
  loop by POSTing to a relay URL: a server-to-server caller, a CLI agent. (In the
  browser this half is TypeScript — see Position 3b.)
- **Position 3b — React agent.** `@noukai/agent` (TypeScript) is the browser
  front end. Python has **no** React binding by design — see [§6](#6-position-3b--the-browser-front-end-typescript-only).

A complete browser feature is **Position 2 (this Python server) + Position 3b
(the TypeScript browser)**.

---

## 2. Which position am I in?

| Your code… | holds `nk_`? | Position | Use |
|---|---|---|---|
| runs on your server, calls Noukai directly | ✅ yes | 1 — Direct | `flow.execute()` ([README](../README.md#tool-calls)) |
| runs on your server, exposes an endpoint a browser/service calls | ✅ yes | 2 — Server relay | `mount_flow_relay` / `flow_relay_blueprint` — [§4](#4-position-2--serve-a-flow-server-relay) |
| runs server-to-server / in a CLI, no key | ❌ no | 3 — Keyless client | `RelayFlow` / `AsyncRelayFlow` — [§5](#5-position-3--drive-a-flow-keyless-client) |
| a browser / React component | ❌ no | 3b — React agent | `@noukai/agent` (TypeScript) — [§6](#6-position-3b--the-browser-front-end-typescript-only) |

---

## 3. The wire contract (authoritative)

Every position speaks this one contract. The relay forwards it **verbatim**; the
loop produces/consumes it. Source: `SeqflowExecuteRequest` /
`SeqflowExecuteResponse` / `SeqflowExecutePausedResponse` in the router-ai-slugs
service. **Wire field names are `camelCase`** (except inside `messages` /
`toolCallMessages`, which are OpenAI-style `snake_case`).

### 3.1 Fresh call — two modes

**Structured / chat / agent mode** (Pack Maker style — preferred for agents):

```jsonc
POST <relay-url>            // relay forwards to /seq/{org}/{project}/{slug}/execute
{
  "messages": [ { "role": "user", "content": "make me a spelling pack" } ],
  "tools": [ /* OpenAI-style ToolDef */ ],
  "toolChoice": "auto"      // "auto" | "none" | "required" | { … }
}
```

- `messages` roles are **restricted to `user` | `assistant` | `tool`**. A
  `system` or `function` turn → **400 `MESSAGES_ROLE_INVALID`** (it would
  override the flow author's system prompt). The SDK also rejects these
  client-side before the request leaves.
- The **last entry is the current user turn.** History is fed to the model as
  real role turns (not flattened).

**Single-message mode** (Nana style):

```jsonc
{ "message": "hi", "tools": [ … ], "parameters": { "conversation": [ … ] } }
```

Provide **exactly one** of `message` / `messages` on a fresh call. The SDK
enforces this client-side (`validate_fresh_call`).

### 3.2 Yield (HTTP 200) — the flow paused for tools

```jsonc
{
  "status": "tool_calls_required",
  "executionId": "…",
  "pausedAtStep": "…",
  "iterationsUsed": 1,
  "toolCallMessages": [ … ],       // opaque prior-turn state; pass back on resume
  "toolCalls": [ { "id": "call_1", "type": "function",
                   "function": { "name": "lookup", "arguments": "{…}" } } ],
  "accumulatedOutputs": { … },
  "flowId": "…",
  "blockCount": 3
}
```

### 3.3 Resume — send tool results, continue

```jsonc
{
  "executionId": "…",              // required
  "pausedAtStep": "…",             // required   } all three identify the resume
  "toolCallMessages": [ …prev, …toolResults ],  // required
  "iterationsUsed": 1,
  "accumulatedOutputs": { … },
  "tools": [ … ]
}
```

A **tool result** entry uses the camelCase Noukai wire:

```jsonc
{ "role": "tool", "toolCallId": "call_1", "content": "72°F and sunny" }
```

### 3.4 Complete (HTTP 200) — final result

```jsonc
{ "status": "completed", "result": { … }, "flowId": "…", "blockCount": 3 }
```

> **You rarely hand-build any of this.** The loop (`RelayFlow`, or
> `flow.execute`) builds fresh/resume payloads and parses yield/complete for you.
> Hand-build only if writing a *new* keyless client from scratch in a language
> without an SDK. The relay itself **never parses** these beyond bounds checks —
> it is a byte-pipe.

### 3.5 Errors / status codes

| Status | `detail` / code | Origin | Meaning |
|---|---|---|---|
| 400 | `INVALID_JSON` | **relay** | body was not a JSON object |
| 400 | `MESSAGES_ROLE_INVALID` | server | a `system`/`function` role in `messages` |
| 403 | `FORBIDDEN` (or your framework's detail) | **relay** | `authorize` hook raised |
| 413 | `BODY_TOO_LARGE` | **relay** | raw bytes > `max_body_bytes` (before parse) |
| 413 | `TOO_MANY_MESSAGES` | **relay** | `messages`/`toolCallMessages` length > `max_messages` |
| 413 | `MESSAGES_TOO_LARGE` | server | payload > 1 MB |
| 4xx | `TOOLS_NOT_ENABLED`, `TOOL_ITERATION_LIMIT`, `TOOLS_REQUIRE_SYNC_EXECUTE` | server | tool-config errors |
| 502 | `UPSTREAM_UNAVAILABLE` | **relay** | connection/timeout to Noukai (no upstream status to relay) |
| *upstream* | `UPSTREAM_NON_JSON` | **relay** | upstream returned a non-JSON body; relayed at the upstream status |

The relay passes every **server** status/body through unchanged. It only
*originates* the rows marked **relay**. (The `authorize`-raised exception itself
is mapped by your framework — FastAPI/Flask — so its status/detail is whatever
you raised, e.g. `HTTPException(status_code=403, detail="…")`.)

---

## 4. Position 2 — serve a flow (server relay)

Your key-holding server exposes a keyless endpoint. The relay: reads the raw body
→ bounds it (bytes **before** parse, streamed so an oversize body is aborted
mid-read, then message counts) → `await authorize(request)` → forwards verbatim
to `/seq/{org}/{project}/{slug}/execute` with the `nk_` bearer
(`raise_for_status=False`) → relays the upstream `(status, body)`.

The flow is **pinned** to one `org/project/slug` — a caller cannot redirect the
relay to another flow.

### 4.1 FastAPI / Starlette (async)

Use an **`AsyncNoukai`** client.

```python
import os
from fastapi import FastAPI, HTTPException, Request
from noukai_sdk import AsyncNoukai
from noukai_sdk.adapters.relay import mount_flow_relay

noukai = AsyncNoukai(api_key=os.environ["NOUKAI_API_KEY"])  # holds nk_ server-side
app = FastAPI()

async def require_maker(request: Request) -> None:
    # App authorization — raise to reject. NEVER bake this into the SDK.
    # The raised HTTPException propagates to FastAPI unchanged (status + detail).
    if request.headers.get("x-role") != "maker":
        raise HTTPException(status_code=403, detail="maker role required")

mount_flow_relay(
    app,
    client=noukai,
    org="acme",
    project="spelling",
    slug="pack-maker",
    path="/agent/execute",          # default
    authorize=require_maker,
    max_body_bytes=262_144,         # 256 KiB — checked on raw bytes before parse
    max_messages=40,                # caps messages / toolCallMessages length
)
```

### 4.2 Flask (sync)

Use a sync **`Noukai`** client.

```python
from flask import Flask, abort, request
from noukai_sdk import Noukai
from noukai_sdk.adapters.relay import flow_relay_blueprint, RelayBounds

noukai = Noukai(api_key="nk_...")
app = Flask(__name__)

def require_maker(req) -> None:
    if req.headers.get("x-role") != "maker":
        abort(403, "maker role required")  # raises a werkzeug HTTPException

app.register_blueprint(flow_relay_blueprint(
    client=noukai,
    org="acme",
    project="spelling",
    slug="pack-maker",
    path="/agent/execute",
    authorize=require_maker,
    bounds=RelayBounds(max_body_bytes=262_144, max_messages=40),
))
```

### 4.3 Options

**`mount_flow_relay(app, *, client, org, project, slug, path, authorize, max_body_bytes, max_messages, version)`** (FastAPI)
· **`flow_relay_blueprint(*, client, org, project, slug, path, authorize, bounds, version)`** (Flask)

| Option | Type | Default | Notes |
|---|---|---|---|
| `client` | `AsyncNoukai` (FastAPI) / `Noukai` (Flask) | — | **required.** Holds the `nk_` bearer injected on the forward. |
| `org`, `project`, `slug` | `str` | — | **required.** The single flow this relay is pinned to. |
| `authorize` | `async (request)->None` / `(request)->None` | — | **required.** Raise to reject (`HTTPException` / `abort`); the exception propagates to the framework unchanged. |
| `path` | `str` | `"/agent/execute"` | Route to mount. |
| `max_body_bytes` (FastAPI) | `int` | `262144` | Raw-byte cap, before parse. |
| `max_messages` (FastAPI) | `int` | `40` | Caps each of `messages` / `toolCallMessages`. |
| `bounds` (Flask) | `RelayBounds` | `RelayBounds()` = 256 KiB / 40 | `RelayBounds(max_body_bytes=…, max_messages=…)`. |
| `version` | `"draft" \| int` | `"draft"` | Draft, or a published integer version. `"production"` raises `ValueError`. |

Import surface:
`from noukai_sdk.adapters.relay import mount_flow_relay, flow_relay_blueprint, RelayBounds`.
Also exported: `DEFAULT_RELAY_PATH`, `DEFAULT_MAX_BODY_BYTES`, `DEFAULT_MAX_MESSAGES`.
Framework imports are **lazy** — the SDK does not hard-depend on Starlette/Flask;
`mount_flow_relay` raises `ImportError` if Starlette/FastAPI is absent, and
`flow_relay_blueprint` if Flask is absent.

### 4.4 Security invariants — non-negotiable

1. **App authorization lives in `authorize`, never in the SDK.** Role checks,
   session/JWT validation, rate-limit gates → all in the hook.
2. **Bounds are adapter config, never SDK-wide constants.** Tune `max_body_bytes`
   / `max_messages` per deployment.
3. **The relay never logs the key or the body.** Do **not** enable `log_payloads`
   on the relay's client — that would route the forwarded browser payload and
   upstream body to your log handler. (The `nk_` key is never logged regardless.)
4. **Verbatim passthrough.** The relay does not interpret business payloads and
   does not raise typed errors in place of relaying status (it forwards with
   `raise_for_status=False`). A connection/timeout to Noukai surfaces as
   `502 {"detail": "UPSTREAM_UNAVAILABLE"}` (no upstream status to relay).
5. **The flow is pinned.** `org/project/slug` are fixed at mount; a caller can
   never point the relay at a different flow.

### 4.5 Common mistakes

- ❌ Passing a sync `Noukai` to `mount_flow_relay` (needs `AsyncNoukai`) or an
  `AsyncNoukai` to `flow_relay_blueprint` (needs sync `Noukai`).
- ❌ Returning a value from `authorize` to reject — you must **raise** (the
  framework maps the exception to the HTTP response).
- ❌ Putting the maker-role / auth logic anywhere but `authorize`.

Runnable: [`examples/relay_fastapi.py`](../examples/relay_fastapi.py).

---

## 5. Position 3 — drive a flow (keyless client)

Code with **no** `nk_` key drives the same loop by POSTing to a relay URL.
`RelayFlow(url)` (sync) / `AsyncRelayFlow(url)` (async) run the identical
yield/resume loop as `flow.execute()` — same round limit (`10`), same
`PausedResult` — over a keyless relay transport.

```python
from noukai_sdk import RelayFlow, ExecuteResult

flow = RelayFlow("https://your-server.example.com/agent/execute")

def my_tools(tool_calls: list[dict]) -> list[dict]:
    # Execute the model's requested tool calls locally.
    return [
        {"role": "tool", "toolCallId": c["id"], "content": f"result-for-{c['id']}"}
        for c in tool_calls
    ]

# Auto-resume: give a tool_handler and the loop runs to completion.
result = flow.execute(
    messages=[{"role": "user", "content": "make me a spelling pack"}],
    tools=[{"type": "function", "function": {"name": "lookup", "description": "look up a word"}}],
    tool_choice="auto",
    tool_handler=my_tools,      # omit to get a PausedResult you drive manually
)
assert isinstance(result, ExecuteResult)
print(result.status, result.result)
```

Async mirror — accepts sync **or** async `tool_handler`:

```python
from noukai_sdk import AsyncRelayFlow

flow = AsyncRelayFlow("https://your-server.example.com/agent/execute")
result = await flow.execute(messages=[{"role": "user", "content": "…"}], tools=[...], tool_handler=my_tools)
```

### 5.1 `.execute()` parameters (both `RelayFlow` and `AsyncRelayFlow`)

| Param | Type | Notes |
|---|---|---|
| `message` | `str` | Single-message mode (first positional). Mutually exclusive with `messages`. |
| `messages` | `list[ChatMessage \| dict]` | Structured mode. Roles `user\|assistant\|tool` only. |
| `tools` | `list[dict]` | OpenAI-style tool defs. |
| `tool_choice` | `"auto" \| "none" \| "required" \| dict` | |
| `tool_handler` | `(tool_calls) -> tool_results` (async allowed on `AsyncRelayFlow`) | Omit → returns a `PausedResult`. |
| `max_tool_rounds` | `int` | Default `10`; exceeding raises `ToolCallLimitError`. |
| `parameters` | `dict` | Passed through (e.g. `{"conversation": [...]}` in single-message mode). |
| `trace` | `bool` | Default `False`. |

Constructor: `RelayFlow(url, *, client=None, timeout=None)` — pass a custom
`httpx` client or per-request timeout if needed. `AsyncRelayFlow` mirrors it.

> A sync `RelayFlow` **rejects an async `tool_handler`** with `TypeError` — use
> `AsyncRelayFlow` for coroutine handlers.

### 5.2 Manual resume (no `tool_handler`)

```python
result = flow.execute(messages=[{"role": "user", "content": "…"}], tools=[...])
while result.requires_tool_calls:                       # True → PausedResult
    tool_results = run_tools(result.tool_calls)
    result = result.resume_sync(tool_results=tool_results)   # async: await result.resume(...)
print(result.result)
```

`result.requires_tool_calls` distinguishes the two: `True` → `PausedResult` (has
`.tool_calls`, `.resume_sync(...)` / `await .resume(...)`); `False` →
`ExecuteResult` (has `.result`). A `PausedResult` from a **sync** `RelayFlow`
must be resumed with `.resume_sync(...)`; from an **async** one with
`await .resume(...)`.

Runnable: [`examples/relay_client.py`](../examples/relay_client.py).

---

## 6. Position 3b — the browser front end (TypeScript only)

There is **no Python React binding** — the one deliberate cross-language gap
(React is TypeScript). The browser half of an agent feature is the
[`@noukai/agent`](../../noukai-typescript-agent-sdk/README.md) package
(`useAgentChat` / `runAgentLoop`), which drives your Python relay (§4) over
`createRelayFlow`. See the
[TypeScript relay guide §6](../../noukai-typescript-sdk/docs/AGENT_RELAY.md#6-position-3b--react-agent-noukaiagent).

A framework-agnostic **Python** agent peer (`noukai-agent` on PyPI — the
`runAgentLoop` equivalent over `RelayFlow`, a tool registry, no React) is
**designed but not yet built**; it ships when a real Python consumer (a
server-to-server relay or CLI agent) appears. Until then, Position 3 above
(`RelayFlow`) is the Python keyless-client primitive.

> **Aspirational, do not use:** the opaque `stateToken` hardening (so the browser
> stops seeing raw `executionId`/`toolCallMessages` execution-state) is a
> **future** backend-coordinated change (design F5). Until then, relays echo
> server execution-state — acceptable **behind auth + bounds**.

---

## 7. End-to-end recipe (browser feature)

1. **Server (Position 2, this SDK):** mount `mount_flow_relay` (FastAPI) or
   `flow_relay_blueprint` (Flask) at `POST /agent/execute`, pinned to your flow,
   with an `authorize` hook and bounds. Client built with `log_payloads` off.
2. **Browser (Position 3b, TypeScript):** `useAgentChat({ endpoint: "/agent/execute", … })`
   from `@noukai/agent`. Register tools; resolve them locally. For a Python
   keyless *client* (not a browser), use `RelayFlow` (Position 3).
3. **Never** ship the `nk_` key past your server. The client POSTs keyless; the
   relay injects the key.

---

## 8. Implementation checklist (verify before shipping)

- [ ] Correct client type: `AsyncNoukai` for `mount_flow_relay`, sync `Noukai`
      for `flow_relay_blueprint`.
- [ ] `authorize` implements the real check (session/JWT/role) and **raises** to
      reject (`HTTPException` / `abort`).
- [ ] Bounds tuned for the deployment; defaults are 256 KiB / 40.
- [ ] Relay's client has `log_payloads` **off**.
- [ ] Flow is pinned (`org/project/slug` fixed); caller cannot redirect it.
- [ ] Keyless client holds **no** `nk_` key and points at the relay URL.
- [ ] Tool results returned as `{"role": "tool", "toolCallId": …, "content": …}`.
- [ ] `messages` uses only `user`/`assistant`/`tool` roles.
- [ ] Round limit understood (`10`; `ToolCallLimitError` on overflow).
- [ ] Sync vs async resume method correct (`.resume_sync` vs `await .resume`).
- [ ] Error/status table (§3.5) handled on the client side.
```
