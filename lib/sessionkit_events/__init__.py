"""Session event store and operator attention queue."""

from .queue import STALE_AFTER_MS, attention_queue, build_attention_queue
from .store import (
    EVENTS,
    SOURCES,
    EventError,
    EventStore,
    append_event,
    events_root,
    mark_seen,
    now_unix_ms,
    read_events,
    thread_key,
    valid_thread_key,
)

__all__ = [
    "EVENTS",
    "SOURCES",
    "STALE_AFTER_MS",
    "EventError",
    "EventStore",
    "append_event",
    "attention_queue",
    "build_attention_queue",
    "events_root",
    "mark_seen",
    "now_unix_ms",
    "read_events",
    "thread_key",
    "valid_thread_key",
]
