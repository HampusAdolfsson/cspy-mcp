"""Parsing of the JSON-string arguments MCP tools take."""

from __future__ import annotations

import json
from typing import Any, Callable

from iar_cspy import CSpyError, Context


def parse_json(raw: str, label: str = "args_json") -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CSpyError(f"Invalid JSON in {label}: {exc}") from exc


def call_with_json_args(fn: Callable[..., Any], method: str, args_json: str) -> Any:
    """Call ``fn(method, ...)`` with args from JSON.

    A list is positional arguments, an object keyword arguments, and any
    other value one positional argument.
    """
    parsed = parse_json(args_json)
    if isinstance(parsed, list):
        return fn(method, *parsed)
    if isinstance(parsed, dict):
        return fn(method, **parsed)
    return fn(method, parsed)


def context_from_json(context_json: str | None) -> Context | dict[str, Any] | None:
    """``context_json`` as the API takes it; empty means the current base context."""
    if not context_json:
        return None
    obj = parse_json(context_json, "context_json")
    if not isinstance(obj, dict):
        raise CSpyError("context_json must be a JSON object")
    return obj
