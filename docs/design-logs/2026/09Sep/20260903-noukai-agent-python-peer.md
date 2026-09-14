# 20260903 — `noukai-agent` (Python peer) — surface design, build DEFERRED

- **Status:** Surface designed; **build deferred** (no Python consumer yet).
- **Parent:** `20260903-SDK-agent-relay` (PR-3, Python peer / Option B).
- **Depends on:** `noukai-sdk >= 0.5.0` (ships `RelayFlow`/`AsyncRelayFlow`,
  `RelayExecuteTransport`, `ChatMessage`, the `ExecuteTransport` seam).

## Why deferred

Option B for TypeScript is a standalone `@noukai/agent` that *depends on*
`@noukai/sdk`. The Python mirror would be a standalone **`noukai-agent`** on
PyPI depending on `noukai-sdk`. Per the parent design it is **gated on a real
Python consumer** (a server-to-server relay driver or a CLI agent) — we do not
build speculatively.

Today there is **no such consumer**:

- `nouko-pack-agent` is the relay **server** (keyholder proxy) — it adopts
  PR-1's `mount_flow_relay`, it does not *drive* the client loop.
- The framework-agnostic client loop primitive Python needs **already ships in
  `noukai-sdk` 0.5.0**: `RelayFlow(url)` / `AsyncRelayFlow(url)` run the shared
  yield/resume loop over a keyless `RelayExecuteTransport`, supporting both
  fresh-call modes and `tool_handler` auto-resume.

So the base capability is present; `noukai-agent` would only add the ergonomic
layer that `@noukai/agent` adds on the TS side. Build it when a consumer appears.

Note (per parent): do **not** add `noukai-agent` to `check_parity.py` — that
gate pairs only the two existing SDKs (`noukai-sdk` ↔ `@noukai/sdk`). The Python
agent peer is versioned independently, mirroring `@noukai/agent`.

## Proposed surface (mirrors `@noukai/agent`, minus the React hook)

```python
# noukai_agent/__init__.py  (package: noukai-agent, import: noukai_agent)

from noukai_sdk import RelayFlow, AsyncRelayFlow, ChatMessage  # re-exported

# --- Tool wire-adapters (internal <-> OpenAI tool wire format) ----------------
def to_wire_tool_defs(defs: list[ToolDefinition]) -> list[dict]: ...
def parse_wire_tool_call(raw: dict) -> ToolCall: ...
def to_wire_tool_result(call_id: str, content: str) -> dict: ...

# --- Types (thin dataclasses / TypedDicts) ------------------------------------
@dataclass
class ToolDefinition: name: str; description: str; parameters: dict
@dataclass
class ToolCall: id: str; name: str; arguments: dict
@dataclass
class ToolResult: tool_call_id: str; result: str
ToolResolver = Callable[[ToolCall], ToolResult | Awaitable[ToolResult]]

# --- Tool registry (declarative register/resolve) -----------------------------
class ToolRegistry:
    def register(self, *, definition: ToolDefinition, resolve: ToolResolver) -> None: ...
    def definitions(self) -> list[ToolDefinition]: ...
    def resolve(self, call: ToolCall) -> ToolResult: ...           # sync
    async def aresolve(self, call: ToolCall) -> ToolResult: ...    # async

# --- The loop convenience (runAgentLoop equivalent) ---------------------------
# Thin wrapper over RelayFlow/AsyncRelayFlow that adds: local resolution,
# duplicate-call de-dup, an on_tool_call_start progress hook, and result
# unwrapping — exactly the value-adds @noukai/agent layers on createRelayFlow.

@dataclass
class AgentLoopResult:
    type: Literal["message", "max_iterations"]
    content: str | None = None
    metadata: Any | None = None

def run_agent_loop(
    *,
    endpoint: str,
    message: str | None = None,
    messages: list[ChatMessage | dict] | None = None,
    tools: list[ToolDefinition],
    resolve_tool_call: ToolResolver,
    max_rounds: int = 10,                 # = noukai_sdk DEFAULT_MAX_TOOL_ROUNDS
    on_tool_call_start: Callable[[list[ToolCall]], None] | None = None,
    parameters: dict | None = None,
    tool_choice: Any | None = None,
    client: Any = None,                   # optional injected httpx.Client
) -> AgentLoopResult: ...

async def arun_agent_loop(... same ...) -> AgentLoopResult: ...   # async twin
```

### Implementation sketch (when built)

`run_agent_loop` builds `RelayFlow(endpoint, client=client)` (async:
`AsyncRelayFlow`), wraps `resolve_tool_call` in a `tool_handler` closure that
de-dups by `(name, args)`, fires `on_tool_call_start` for fresh calls, and maps
each result via `to_wire_tool_result`. It passes `tool_handler` to
`RelayFlow.execute(...)`, then maps the returned `ExecuteResult` into
`AgentLoopResult` (unwrapping `{content}` / `{message}` shapes), and catches
`ToolCallLimitError` → `AgentLoopResult(type="max_iterations")`. All of the
loop, models, transport, round limit, and error taxonomy come from `noukai-sdk`
— `noukai-agent` never re-declares them (the F1 discipline).

### Packaging (when built)

- New repo/dir `noukai-agent-python-sdk`, PyPI `noukai-agent`, import
  `noukai_agent`; `dependencies = ["noukai-sdk>=0.5,<1.0"]`; own CHANGELOG /
  RELEASING; **not** in `check_parity.py`.
- No React equivalent (the one legitimate cross-language gap — Python has no UI
  binding; a Python consumer is a server/CLI).

## Explicitly deferred

- The entire `noukai-agent` PyPI package build (above). Ship when a real Python
  consumer (server-to-server relay driver or CLI agent) exists.
- No changes to `noukai-sdk` are required to build it later — the 0.5.0 surface
  (`RelayFlow`/`AsyncRelayFlow`, `RelayExecuteTransport`, `ChatMessage`) is the
  foundation it will consume.
