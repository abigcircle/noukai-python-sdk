# Integration Tests

Tests that exercise the Noukai Python SDK against a real Noukai server.
Skipped by default — no server required for `pytest tests/unit/`.

## When to Run

- Locally before pushing significant SDK changes
- In CI nightly and on tag releases (see `.github/workflows/integration.yml`)
- After server-side changes to `executor/router-ai-slugs`
- After authoring or modifying fixture flows

## What Is Tested

| File | What it exercises |
|------|-------------------|
| `test_execute.py` | `flow.execute()` happy path, parameters, trace flag, cost_usd type, async parity |
| `test_execute_async.py` | Queue-backed `execute_async()`, `Job.poll()`, `Job.wait()`, timeout |
| `test_steps.py` | `flow.steps()` step-by-step iteration, cursor management, async parity |
| `test_events.py` | Raw SSE event stream, event ordering, token fields, run_remaining=True |
| `test_tool_calls.py` | Auto tool handler, manual resume, max_tool_rounds limit, async handler |
| `test_run_proxy.py` | Trace endpoints — **all xfail** until server prereq lands |
| `test_errors.py` | Auth error, slug-not-found, zero-credit account, request_id propagation |

## Setup

### 1. Get an API Key

Mint an `nk_*` key from the Noukai dashboard:
- Go to **Settings → API Keys** in your project
- Click **New API Key**, copy the `nk_...` value

See the [Noukai Docs — API Keys](https://docs.noukai.xyz/concepts/api-keys) for
full instructions.

### 2. Create the Fixture Project and Flows

The tests require a dedicated project (`org/project`) with three known flows.
See `fixtures/README.md` for what each flow should do and how to author them
via `noukai-mcp`.

| Flow | Env var | What it does |
|------|---------|--------------|
| `hello-world` | `NOUKAI_INTEGRATION_HELLO_SLUG` | Single LLM block, echoes message |
| `two-step` | `NOUKAI_INTEGRATION_TWO_STEP_SLUG` | Two sequential LLM blocks |
| `tools-enabled` | `NOUKAI_INTEGRATION_TOOLS_SLUG` | LLM block with `get_weather` tool |

### 3. Configure Environment Variables

**Recommended**: copy the example file and edit values in place.

```bash
cd development/noukai/sdk/python
cp .env.example .env
# edit .env with your real key/project/slugs
```

`tests/integration/conftest.py` auto-loads `.env` from the SDK root via
`python-dotenv` (a dev dependency). No need to `source` it manually.

**Alternative**: export inline (no `.env` file):

```bash
export NOUKAI_ENV=dev   # for localhost:8080; omit for production
export NOUKAI_INTEGRATION_KEY="nk_..."
export NOUKAI_INTEGRATION_PROJECT="your-org/integration-tests"
export NOUKAI_INTEGRATION_HELLO_SLUG="hello-world"
export NOUKAI_INTEGRATION_TWO_STEP_SLUG="two-step"
export NOUKAI_INTEGRATION_TOOLS_SLUG="tools-enabled"
```

The `.env` file is in `.gitignore` — never commit it.

### 4. Run the Tests

```bash
# Against local dev server (NOUKAI_ENV=dev → localhost:8080)
NOUKAI_ENV=dev \
NOUKAI_INTEGRATION_KEY=nk_... \
NOUKAI_INTEGRATION_PROJECT=your-org/integration-tests \
NOUKAI_INTEGRATION_HELLO_SLUG=hello-world \
NOUKAI_INTEGRATION_TWO_STEP_SLUG=two-step \
NOUKAI_INTEGRATION_TOOLS_SLUG=tools-enabled \
uv run pytest tests/integration/ -v

# Against staging/production — drop NOUKAI_ENV
NOUKAI_INTEGRATION_KEY=nk_prod_... \
NOUKAI_INTEGRATION_PROJECT=your-org/integration-tests \
NOUKAI_INTEGRATION_HELLO_SLUG=hello-world \
NOUKAI_INTEGRATION_TWO_STEP_SLUG=two-step \
NOUKAI_INTEGRATION_TOOLS_SLUG=tools-enabled \
uv run pytest tests/integration/ -v

# Run a single test file
uv run pytest tests/integration/test_execute.py -v

# Run skipping tool-call tests (if tools fixture not set up)
uv run pytest tests/integration/ -v -k "not tool_calls"
```

## Running Without Env Vars

If no `NOUKAI_INTEGRATION_*` env vars are set, the entire integration suite
skips cleanly:

```bash
uv run pytest tests/integration/ -v
# → all tests: SKIPPED (Integration env vars not set; see tests/integration/README.md)
```

Unit tests are never affected:
```bash
uv run pytest tests/unit/ -v
# → runs normally, no server required
```

## Optional: Zero-Credit Error Test

`test_errors.py::test_zero_credits_raises_insufficient_credits` requires a
separate test account with exactly zero credits:

```bash
export NOUKAI_INTEGRATION_ZERO_CREDIT_KEY="nk_..."
export NOUKAI_INTEGRATION_ZERO_CREDIT_PROJECT="zero-credit-org/project"
export NOUKAI_INTEGRATION_ZERO_CREDIT_SLUG="hello-world"
```

Without these vars the test skips automatically.

## What Is Blocked

`test_run_proxy.py` contains tests marked `xfail` for trace endpoint coverage.
These tests ARE written (not commented-out) and will auto-pass once the
server-side prerequisite lands:

**Server prereq:** slug-scoped trace endpoints
```
GET /seq/{org}/{project}/{slug}/runs/{id}/trace
GET /seq/{org}/{project}/{slug}/runs/{id}/steps/{step_id}/trace
GET /seq/{org}/{project}/{slug}/runs/{id}/trace/stream
```
must accept `nk_*` API keys (currently only Supabase JWT is supported on these
endpoints on some deployments).

Once deployed, remove the `xfail` marks from `test_run_proxy.py`.

## CI

Integration tests run automatically in GitHub Actions:
- **Nightly** at 06:00 UTC (`cron: "0 6 * * *"`)
- **On tag push** matching `v*.*.*`
- **Manually** via `workflow_dispatch`

See `.github/workflows/integration.yml` for the full configuration.

Required GitHub secrets/vars:
- `NOUKAI_INTEGRATION_KEY` (secret)
- `NOUKAI_INTEGRATION_PROJECT` (var)
- `NOUKAI_INTEGRATION_HELLO_SLUG` (var)
- `NOUKAI_INTEGRATION_TWO_STEP_SLUG` (var)
- `NOUKAI_INTEGRATION_TOOLS_SLUG` (var)
