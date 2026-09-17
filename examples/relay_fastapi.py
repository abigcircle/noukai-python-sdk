"""Runnable example: serve a flow to a browser via the relay adapter.

Design: 20260903-SDK-agent-relay (PR-1).

Your server holds the ``nk_`` key and exposes a keyless ``POST /agent/execute``
that a browser drives. The relay bounds abuse, runs your ``authorize`` hook,
then forwards the request verbatim to the flow's ``/execute`` endpoint and
relays the upstream response back unchanged.

Run it::

    pip install "noukai-sdk[fastapi]" uvicorn
    NOUKAI_API_KEY=nk_... uvicorn examples.relay_fastapi:app --port 8000

Then, as a browser would (note: no key, and X-Role gates the maker check)::

    curl -sS localhost:8000/agent/execute \
      -H 'content-type: application/json' -H 'x-role: maker' \
      -d '{"messages":[{"role":"user","content":"make me a spelling pack"}],
           "tools":[],"toolChoice":"auto"}'

    # Missing/instead-wrong role → 403 from your authorize hook:
    curl -i localhost:8000/agent/execute -H 'content-type: application/json' \
      -d '{"message":"hi"}'
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Request

from noukai_sdk import AsyncNoukai
from noukai_sdk.adapters.relay import mount_flow_relay

# The client holds the nk_ bearer server-side; the browser never sees it.
noukai = AsyncNoukai(api_key=os.environ.get("NOUKAI_API_KEY", "nk_example"))

app = FastAPI()


async def require_maker(request: Request) -> None:
    """App authorization — raise to reject. This stays in your app, never in the
    SDK. Here we gate on a header; a real app checks a session / JWT / role."""
    if request.headers.get("x-role") != "maker":
        raise HTTPException(status_code=403, detail="maker role required")


mount_flow_relay(
    app,
    client=noukai,
    org="acme",
    project="spelling",
    slug="pack-maker",
    path="/agent/execute",
    authorize=require_maker,
    max_body_bytes=262_144,  # 256 KiB — checked on raw bytes before parse
    max_messages=40,  # caps messages[] / toolCallMessages[] length
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
