"""Pydantic config helpers for snake_case ↔ camelCase wire translation.

Server uses camelCase JSON aliases (executionId, flowId, blockCount, etc.)
SDK exposes snake_case attributes. Both directions supported."""

from pydantic import ConfigDict

WIRE_CONFIG = ConfigDict(
    populate_by_name=True,
    extra="ignore",
)
