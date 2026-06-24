# noukai-sdk

Python SDK for executing [Noukai](https://noukai.xyz) flows.

Sync and async clients, fully typed (`py.typed`), Pydantic models for results and events, supports Python 3.10+.

- [Install](#install)
- [Quick start](#quick-start)
- [Authentication](#authentication)
- [Client configuration](#client-configuration)
- [Executing flows](#executing-flows)
- [Streaming steps and events](#streaming-steps-and-events)
- [Async (queue-backed) jobs](#async-queue-backed-jobs)
- [Tool calls](#tool-calls)
- [Replay and session grouping (experimental)](#replay-and-session-grouping-experimental)
- [Flow versions](#flow-versions)
- [Run traces](#run-traces)
- [Errors](#errors)
- [Timeouts, retries](#timeouts-retries)
- [Logging](#logging)
- [Resource management](#resource-management)

## Install

```bash
pip install noukai-sdk
# or
uv add noukai-sdk
# or
poetry add noukai-sdk
```

Requires Python 3.10+. Dependencies: `httpx`, `pydantic` v2.

## Quick start

### Synchronous

```python
from noukai_sdk import Noukai

with Noukai() as client:                                  # reads NOUKAI_API_KEY
    result = client.flow("acme/spelling/grade-3").execute(
        message="The cat sat on the mat.",
    )
    print(result.result)
```

### Asynchronous

```python
import asyncio
from noukai_sdk import AsyncNoukai

async def main() -> None:
    async with AsyncNoukai() as client:
        result = await client.flow("acme/spelling/grade-3").execute(
            message="The cat sat on the mat.",
        )
        print(result.result)

asyncio.run(main())
```

The context manager auto-releases the underlying HTTP pool. If you can't use one, call `client.close()` / `await client.aclose()` manually.

## Authentication

API keys start with `nk_`. Provide one of:

1. **`NOUKAI_API_KEY` environment variable** — recommended.
2. **`api_key` constructor argument** — overrides the env var.

```python
client = Noukai(api_key="nk_...")
```

If no key is found, the constructor raises `AuthenticationError` immediately.

## Client configuration

Both `Noukai` and `AsyncNoukai` accept the same arguments:

```python
client = Noukai(
    api_key="nk_...",          # overrides NOUKAI_API_KEY
    env="production",          # or "dev" — default "production"
    org="acme",                # default org for short-form slugs
    project="spelling",        # default project for short-form slugs
    timeout=300.0,             # default per-request timeout (seconds)
    max_retries=1,             # retries on retryable 5xx
    log_handler=my_logger,     # structured logging hook
    log_payloads=False,        # include request/response bodies in logs
)
```

### `env` shortcut

| Value          | Base URL                                  |
| -------------- | ----------------------------------------- |
| `"production"` (default) | `https://api.noukai.xyz/api/v1` |
| `"dev"`        | `http://localhost:8080/api/v1`            |

Falls back to the `NOUKAI_ENV` env var. The SDK does **not** accept an arbitrary base URL — all requests target Noukai's hosted endpoints.

### Default `org` / `project`

When you set both, you can address flows by their short slug:

```python
client = Noukai(org="acme", project="spelling")

client.flow("grade-3")                    # → acme/spelling/grade-3
client.flow("other-org/x/grade-3")        # fully qualified — wins
client.flow(org="acme", project="spelling", slug="grade-3")  # explicit
```

`org` and `project` must be passed together (or not at all). Half-defaults raise `ValueError`.

## Executing flows

### Three identifier forms

```python
client.flow("grade-3")                                # uses client defaults
client.flow("acme/spelling/grade-3")                  # fully qualified
client.flow(org="acme", project="spelling", slug="grade-3")
```

### `execute()` — synchronous, in-process

Blocks until the flow completes (or pauses for tools).

```python
result = client.flow("acme/spelling/grade-3").execute(
    message="Input text",
    parameters={"difficulty": "hard"},          # extra initial inputs
    block_overrides={"step-id": {"temperature": 0.5}},
    attachments=[{"url": "https://...", "mime_type": "image/png"}],
    trace=False,                                # capture full I/O for trace
    version="draft",                            # or an int published version
    timeout=60.0,                               # override client default
)

if result.status == "tool_calls_required":
    # PausedResult — see "Tool calls" below
    ...
else:
    print(result.result)                        # ExecuteResult
```

Returns `ExecuteResult | PausedResult`. The same surface exists on `AsyncFlow.execute` — just `await` it.

## Streaming steps and events

For long-running flows, stream output as it arrives. The sync client returns `Iterator`; the async client returns `AsyncIterator`. Both honour early termination (`break`, `return`).

### `steps()` — one event per finished step

```python
flow = client.flow("acme/spelling/grade-3")

for step in flow.steps(message="..."):
    print(step.name, step.output, step.duration_ms, step.tokens)
```

Async:

```python
async for step in flow.steps(message="..."):
    ...
```

Yields `StepCompleted` events only. Intermediate signals are filtered out.

### `events()` — every typed SSE event

```python
for event in flow.events(message="..."):
    match event.type:
        case "run_started":            # RunStarted
            ...
        case "step_started":           # StepStarted
            ...
        case "step_input":             # StepInput
            ...
        case "step_output":            # StepOutput
            ...
        case "step_completed":         # StepCompleted
            ...
        case "step_error":             # StepFailed
            ...
        case "step_paused":            # StepPaused
            ...
        case "tool_calls_required":    # ToolCallsRequired (has .resume())
            ...
        case "flow_completed":         # FlowCompleted
            ...
```

Pass `run_remaining=True` to have the server stream every remaining step in a single SSE connection instead of pausing between steps.

## Async (queue-backed) jobs

For long executions where you don't want to hold an HTTP connection open, submit to the server's job queue and poll for the result.

> **Naming note.** `execute_async()` refers to **server-side** queue-backed execution, not Python `async`. It exists on both `Flow` (sync) and `AsyncFlow` (async). `AsyncFlow.execute_async()` means "await the submission of a server-side-async job" — both layers are real and useful.

```python
job = client.flow("acme/spelling/grade-3").execute_async(
    message="Long input that takes minutes...",
    trace=True,
)

print(job.execution_id)  # persist this if you want to resume later

# Block until done — polls under the hood (default: 2s interval, 5min timeout).
status = job.wait(timeout=600.0, poll_interval=5.0)
print(status.result)

# Or one-shot:
snapshot = job.poll()
if snapshot.status == "completed":
    ...
```

Async equivalent:

```python
job = await client.flow("acme/spelling/grade-3").execute_async(message="...")
status = await job.wait(timeout=600.0)
```

Tool calls are **not** supported on this path (server-side limitation).

## Tool calls

Flows can pause to request tool execution from your code. The SDK has two modes.

### Auto-resume (recommended)

Pass `tool_handler`; the SDK runs your handler against each pending call and resumes automatically until the flow finishes or `max_tool_rounds` is hit.

```python
def my_tools(tool_calls: list[dict]) -> list[dict]:
    return [
        {"tool_call_id": call["id"],
         "output": get_weather(call["function"]["arguments"])}
        for call in tool_calls
    ]

result = flow.execute(
    message="What's the weather in Paris?",
    tools=[{"type": "function", "function": {"name": "get_weather", ...}}],
    tool_handler=my_tools,
    max_tool_rounds=10,        # default — raises ToolCallLimitError if exceeded
)
```

`AsyncFlow.execute` accepts both sync **and** async handlers; `Flow.execute` accepts sync only and raises `TypeError` at call time if given a coroutine function.

### Manual resume

Omit `tool_handler` and drive the loop yourself (async only):

```python
result = await flow.execute(message="...", tools=[...])

while result.status == "tool_calls_required":
    tool_results = await run_tools(result.tool_calls)
    result = await result.resume(tool_results=tool_results)

print(result.result)
```

During streaming, `ToolCallsRequired` events expose the same `.resume(tool_results=...)` method.

## Replay and session grouping (experimental)

The `@noukai_sdk.trace` decorator groups every Noukai SDK call made inside your
route (or any callable) under one session id. In **capture mode** (always-on
when the decorator is present) the SDK tags each outbound request with
`X-Session-Id` so the backend can record a replayable snapshot. In **replay
mode** (gated by `NOUKAI_REPLAY_ENABLED=true`) a single header lets you replay
that recorded session without making live LLM calls.

### Capture (quick start)

```python
import asyncio
import noukai_sdk
from noukai_sdk import AsyncNoukai

noukai = AsyncNoukai(api_key="nk_...", org="acme", project="spelling")

@noukai_sdk.trace
async def handle(message: str) -> dict:
    result = await noukai.flow("grade-3").execute(message=message)
    return {"output": result.result, "session_id": result.session_id}

asyncio.run(handle("The cat sat on the mat."))
```

When used with the **FastAPI** or **Flask** adapters the response also carries
an `X-Noukai-Session` header so callers can grab the session id without any
code changes:

```python
from fastapi import FastAPI
from noukai_sdk.adapters.fastapi import NoukaiTraceMiddleware

app = FastAPI()
app.add_middleware(NoukaiTraceMiddleware, client=noukai)

@app.post("/grade")
@noukai_sdk.trace
async def grade(req: Request) -> dict:
    body = await req.json()
    result = await noukai.flow("grade-3").execute(message=body["text"])
    return {"output": result.result}
```

Flask equivalent:

```python
from flask import Flask
from noukai_sdk.adapters.flask import init_noukai_trace

app = Flask(__name__)
init_noukai_trace(app, client=noukai)
```

### Replay (debugging against your own API)

The goal of replay is debuggability — re-running a recorded execution through
your live API code without hitting any LLMs. Useful for stepping a debugger
through a failed production request, or re-running a known scenario after
editing your handler.

**End-to-end flow:**

1. **Capture the session id during normal traffic.** Capture runs automatically
   whenever a request enters a `@trace`-wrapped route. The FastAPI/Flask
   adapter sets `X-Noukai-Session: <session_id>` on the response — log it or
   surface it through your error reporter so you have the handle to replay
   later.

2. **Start your API in dev with the replay gate on:**

   ```bash
   NOUKAI_REPLAY_ENABLED=true uvicorn app:main
   ```

3. **Call your own API the same way a real client would**, just add the replay
   header pointing at the captured session id. Replace the URL below with
   *your* app's dev URL and the route you wrote in step 1 (here `/grade` on
   the default uvicorn port):

   ```bash
   curl -H 'X-Noukai-Replay: abc-123-def' \
        -H 'Content-Type: application/json' \
        -d '{"text": "anything — the body is not matched against the cassette"}' \
        http://localhost:8000/grade
   ```

   The adapter middleware detects the header, fetches the recorded session via
   `GET /seq/sessions/{id}` (idempotent, retried by the transport), and serves
   every `Flow.execute()` / `steps()` / `events()` call inside the route from
   the cassette. No LLM calls, no charges, deterministic output.

Your handler code runs for real — only the Noukai SDK calls are served from
the cassette. That means you can edit the handler between replays and the
same session id keeps working, as long as your code still makes the same
sequence of Noukai calls. This is the fast path for verifying a bug fix or
iterating on post-processing logic.

> **Non-HTTP contexts.** For background workers, CLI tools, tests, or
> notebooks where there is no inbound request to carry the header, open the
> scope programmatically:
> ```python
> from noukai_sdk import trace_scope
>
> async with trace_scope(
>     transport=noukai._transport,
>     replay_session_id="abc-123-def",
> ):
>     await noukai.flow("grade-3").execute(message="any input")
> ```
> Use `trace_scope_sync` for sync code. The same `NOUKAI_REPLAY_ENABLED` gate
> applies.

### Capture vs replay

| Feature | Capture mode | Replay mode |
|---|---|---|
| Trigger | `@trace` decorator present | `NOUKAI_REPLAY_ENABLED=true` + `X-Noukai-Replay` header |
| LLM calls | Live | None (served from cassette) |
| `X-Session-Id` outbound | Yes (set by SDK) | Yes (same header, replay session id) |
| `X-Noukai-Session` response | Yes (set by adapter) | Yes (same header) |
| Non-determinism | Normal | Eliminated |
| Production safe | Yes | Gated by env var — a stray header in prod is silently ignored |

### Explicit `session_id` kwarg inside a scope (caution)

> **Warning:** passing an explicit `session_id=` to `Flow.execute()` (or any
> flow method) *inside* an active replay scope bypasses the scope's cassette.
> The SDK performs a **one-shot fetch** of the explicit session and replays from
> that session instead of the scope session. This is intentional — it lets you
> mix two sessions in one request — but it is easy to trigger accidentally if
> you have old code that forwards a session id parameter.
>
> Outside a scope, an explicit `session_id=` kwarg simply tags the outbound
> request with `X-Session-Id` (capture only — no replay fetch is performed
> without the env var guard).

### Production safety

Replay is gated behind `NOUKAI_REPLAY_ENABLED=true`. Without this env var an
`X-Noukai-Replay` header in production is silently ignored; the scope opens in
capture mode as normal. This means:

- Deploying the `@trace` decorator to production is safe.
- Replay cannot be enabled by a client-supplied header alone.

### Errors

| Class | When raised |
|---|---|
| `ReplayMissError` | No cassette entry matched the `(slug, position)` key. |
| `ReplayLeftoverError` | Scope exited with unconsumed cassette entries. |
| `ReplayForbiddenError` | Session belongs to a different org/project. |
| `ReplaySessionNotFoundError` | Session id does not exist on the backend. |
| `ReplaySessionExpiredError` | Session TTL has elapsed; snapshots gone. |
| `ReplayInvalidSessionError` | Session payload is structurally malformed. |
| `ReplayNoSnapshotsError` | Session exists but `trace_capture_mode` was `off`. |
| `ReplayDisabledError` | Replay requested but env var not set. |

All extend `ReplayError` which extends `NoukaiError`.

### Caveats

- Capture requires `trace_capture_mode` on the flow or org to be `full` or
  `redacted`. If the mode is `off`, no snapshot is written and replay fails with
  `ReplayNoSnapshotsError`.
- Concurrent same-slug calls within one scope are undefined in v1 — the SDK
  emits a `UserWarning` at runtime if this is detected.
- `execute_async()` (server-side queue jobs) is captured normally but replay is
  **not supported** in v1 — a `ReplayMissError` is raised.
- The idempotent session fetch (`GET /seq/sessions/{id}`) is retried by the
  transport's default retry policy. Cache of fetched sessions across scopes is
  deferred to v1.1.

### Framework adapter install extras

```bash
pip install "noukai-sdk[fastapi]"   # pulls starlette>=0.36
pip install "noukai-sdk[flask]"     # pulls flask>=3.0
```

## Flow versions

| `version`        | Behaviour                                                            |
| ---------------- | -------------------------------------------------------------------- |
| `"draft"` *(default)* | Latest unpublished draft (what you see in the editor).          |
| `<int>`          | A specific published version (e.g. `version=3`).                     |
| `"production"`   | **Not yet supported** — raises `NotImplementedError` at call site.   |

Pin a version when calling from production code; use `"draft"` only in test and preview environments.

## Run traces

Every execution has an `execution_id`. Use it to fetch trace data after the fact.

```python
run = flow.run("exec_abc123")

# Whole-run trace (one attempt per step)
trace = run.trace()
print(trace.summary, trace.steps)

# Single step, optionally a specific attempt
step_trace = run.step_trace("step-1", attempt="latest")
all_attempts = run.step_trace("step-1", attempt="all")

# Live trace stream (replays from DB, then tails Redis)
for event in run.live_trace():
    print(event)
```

Async equivalent uses `await run.trace()`, `await run.step_trace(...)`, and `async for event in run.live_trace()`.

## Errors

All errors extend `NoukaiError` and carry `status_code`, `code`, `execution_id`, `request_id`, and `response_body` attributes for diagnostics.

| Class                       | HTTP    | When                                               |
| --------------------------- | ------- | -------------------------------------------------- |
| `AuthenticationError`       | 401     | Missing or invalid API key. `.www_authenticate` set when server provides it. |
| `PermissionDeniedError`     | 403     | Key lacks access to the requested resource.        |
| `FlowNotFoundError`         | 404     | Slug or execution ID not found.                    |
| `InsufficientCreditsError`  | 402     | Org balance is insufficient or exhausted.          |
| `RateLimitError`            | 429     | Rate limited — `.retry_after` (seconds) when present. |
| `FlowExecutionError`        | 5xx     | Server-side execution failure. Branch on `.code`.  |
| `APIConnectionError`        | n/a     | Network / DNS / TLS failure before any response.   |
| `APITimeoutError`           | n/a     | Request or job-wait exceeded its timeout.          |
| `ToolCallLimitError`        | n/a     | Client-side: `max_tool_rounds` exhausted.          |

```python
from noukai_sdk import FlowExecutionError

try:
    flow.execute(message="...")
except FlowExecutionError as err:
    if err.code == "BYOK_KEY_REJECTED":
        ...
    raise
```

Common `.code` values: `FLOW_NOT_FOUND`, `INSUFFICIENT_CREDITS`, `CREDITS_EXHAUSTED`, `TOOL_ITERATION_LIMIT`, `BYOK_KEY_REJECTED`, `INVALID_TREE`, `NO_STEPS`, `MISSING_MESSAGE`. Full list on the server contract.

## Timeouts, retries

### Timeouts

- **Client default:** `timeout=300.0` (seconds) on the client constructor.
- **Per-call override:** pass `timeout=60.0` to any method.
- Triggers `APITimeoutError`.

### Retries

- **Default:** `max_retries=1` — one retry on retryable 5xx with exponential backoff.
- Non-retryable status codes are surfaced immediately.

## Logging

```python
def log_handler(event: dict) -> None:
    # {"phase": "request" | "response" | "retry",
    #  "method": ..., "path": ..., "attempt": ...,
    #  "status_code": ..., "request_id": ...,
    #  "request_body": ..., "response_body": ...}
    logger.info(event)

client = Noukai(log_handler=log_handler, log_payloads=True)
```

The hook fires on every request, response, and retry attempt. `request_body` and `response_body` are omitted unless `log_payloads=True` — off by default to protect PII and credentials.

## Resource management

The client holds an HTTP connection pool. Release it explicitly when you're done:

```python
# Recommended — context manager:
with Noukai() as client:
    ...

async with AsyncNoukai() as client:
    ...

# Or manually:
client = Noukai()
try:
    ...
finally:
    client.close()        # AsyncNoukai: await client.aclose()
```

`close()` / `aclose()` are safe to call multiple times.

## Documentation

Full guides, API reference, and examples: <https://noukai.xyz/docs/sdk/python/>

## License

MIT — see [LICENSE](LICENSE).
