"""Process-level runtime store for non-serializable workflow objects.

LangGraph checkpoints must contain only serializable state. Runtime objects
(ToolRegistry, LLMProvider) are stored here, keyed by run_id, and looked up
by nodes at execution time — they never enter the checkpoint.
"""

from __future__ import annotations

import threading
from typing import Any

_store: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def set_runtime(run_id: str, **objects: Any) -> None:
    with _lock:
        _store.setdefault(run_id, {}).update(objects)


def get_runtime(run_id: str) -> dict[str, Any]:
    with _lock:
        return dict(_store.get(run_id, {}))


def get(run_id: str, key: str, default: Any = None) -> Any:
    return get_runtime(run_id).get(key, default)


def clear(run_id: str) -> None:
    with _lock:
        _store.pop(run_id, None)
