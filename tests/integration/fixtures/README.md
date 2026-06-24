# Integration Test Fixture Flows

The JSON files in this directory are **reference specifications** for the three
fixture flows that the integration test suite requires. They describe what each
flow should do, what it accepts, and what it returns — but the JSON files
themselves are not used at runtime.

The actual flows **must exist on the Noukai server** (dev or staging/prod) with
the slugs configured via env vars. See `tests/integration/README.md` for the
full setup guide.

---

## Fixture Flows

### `hello-world` (`NOUKAI_INTEGRATION_HELLO_SLUG`)

**Role:** Baseline happy-path fixture. Used by every test file as the default
single-step flow.

**Structure:**
- One LLM block
- Input: `message` (string) — forwarded directly to the LLM prompt
- Output: a short text response

**What it tests:**
- `test_execute.py` — `execute()` returns `ExecuteResult` with the expected fields
- `test_events.py` — SSE event ordering, token fields, `cost_usd` wire type
- `test_run_proxy.py` — trace roundtrip (xfail until server prereq lands)
- `test_errors.py` — error propagation (auth, not-found)

**Authoring note:** Keep this flow simple. One LLM block with a permissive
system prompt (e.g. "You are a helpful assistant. Answer concisely.") is
sufficient. The message is user-supplied by the test.

---

### `two-step` (`NOUKAI_INTEGRATION_TWO_STEP_SLUG`)

**Role:** Multi-step flow for `steps()` and `events()` iteration tests.

**Structure:**
- Two sequential LLM blocks (Block A → Block B)
- Block A receives `message` and produces an intermediate output
- Block B receives Block A's output and produces the final result
- Input: `message` (string)
- Output: final result from Block B

**What it tests:**
- `test_steps.py` — asserts exactly 2 `StepCompleted` events per run
- `test_steps.py` — cursor management (step_ids are distinct, no manual iteration)
- `test_events.py` — used indirectly when two-step is optionally referenced

**Authoring note:** Both blocks should be LLM blocks so token events appear.
A simple paraphrase + summarise chain works well. Avoid loops.

---

### `tools-enabled` (`NOUKAI_INTEGRATION_TOOLS_SLUG`)

**Role:** Tool-call resume fixture. Used exclusively by `test_tool_calls.py`.

**Structure:**
- One LLM block with the `get_weather` function tool configured
- System prompt instructs the model to **always** call `get_weather` before
  answering weather questions (prevents the model from skipping the tool call)
- Input: `message` (string) — a weather question (e.g. "What is the weather in Tokyo?")
- Output: the model's final answer after receiving tool results

**What it tests:**
- `test_tool_calls.py` — auto tool handler loop
- `test_tool_calls.py` — manual `PausedResult.resume_sync()` path
- `test_tool_calls.py` — `max_tool_rounds` limit via `ToolCallLimitError`
- `test_tool_calls.py` — async `tool_handler` coroutine awaited by SDK

**Authoring note:** The system prompt is critical. Without it, the LLM may
answer without calling the tool, which would cause the tool-call tests to skip
or fail. A working system prompt:

```
You are a weather assistant. When the user asks about the weather, you MUST
call the get_weather function before providing any answer. Do not guess the
weather — always use the tool.
```

---

## How to Author These Flows

Use the `noukai-mcp` tools from within Claude Code (or any MCP client):

```
# 1. Create the project (if it doesn't exist)
create_project org=<your-org> name=integration-tests

# 2. Create each flow and add blocks
create_flow project=integration-tests name=hello-world slug=hello-world
add_block flow=hello-world type=llm name="Echo"

create_flow project=integration-tests name=two-step slug=two-step
add_block flow=two-step type=llm name="Step A"
add_block flow=two-step type=llm name="Step B"

create_flow project=integration-tests name=tools-enabled slug=tools-enabled
add_block flow=tools-enabled type=llm name="Weather" tools=[...]
```

Once the flows work end-to-end, you can export the project definitions via
`hydrate_project` for reference — but the JSON files here are documentation,
not importable artefacts.

---

## Future: `seed-integration` Helper Script

A future `scripts/seed_integration.py` helper script could:
1. Read these JSON files as specifications
2. Call the Noukai API (or `noukai-mcp`) to create the fixture flows
3. Output the env vars needed to point the test suite at the created flows

This would automate onboarding for new contributors who want to run the
integration suite without manually authoring flows via the UI or MCP.

**Status:** Not yet implemented. Manual flow authoring is the current path.
