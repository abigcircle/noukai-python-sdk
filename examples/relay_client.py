"""Runnable example: drive a tool-calling flow through a relay, keyless.

Design: 20260903-SDK-agent-relay (PR-2).

``RelayFlow`` runs the SAME yield/resume loop as ``flow.execute()`` — but over a
keyless relay endpoint (no ``nk_`` key). This is the server-to-server / CLI agent
entrypoint: your code drives the loop and executes tools locally; a keyholder
relay (see ``examples/relay_fastapi.py``) injects the key and forwards to the
flow's ``/execute``.

Run the relay server first (examples/relay_fastapi.py), then::

    python examples/relay_client.py
"""

from __future__ import annotations

from typing import Any

import httpx

from noukai_sdk import ExecuteResult, RelayFlow

RELAY_URL = "http://127.0.0.1:8000/agent/execute"


def my_tools(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Execute the model's requested tool calls locally (stub)."""
    return [
        {"role": "tool", "toolCallId": call["id"], "content": "tool-result-for-" + call["id"]}
        for call in tool_calls
    ]


def main() -> None:
    # The relay's `authorize` hook (see examples/relay_fastapi.py) gates on an
    # `x-role: maker` header. Attach it via an injected httpx client — the
    # keyless RelayFlow has no header hook of its own.
    flow = RelayFlow(RELAY_URL, client=httpx.Client(headers={"x-role": "maker"}))

    # Structured chat/agent mode — the loop auto-resumes through the relay.
    result = flow.execute(
        messages=[{"role": "user", "content": "make me a spelling pack"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "lookup", "description": "look something up"},
            }
        ],
        tool_choice="auto",
        tool_handler=my_tools,  # omit to get a PausedResult you drive with .resume_sync()
    )

    assert isinstance(result, ExecuteResult)
    print("status:", result.status)
    print("result:", result.result)


if __name__ == "__main__":
    main()
