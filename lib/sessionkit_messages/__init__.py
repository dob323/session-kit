"""Session Kit mass messaging: ledger, envelope, and the two delivery paths.

The package is deliberately separate from ``sessionkit_inventory``: the
inventory is a read-only observer of sessions, while this package writes an
operator ledger and speaks to live providers. Only a handful of inventory
primitives are imported (atomic state writes, identity proof), never the
collector's internals.
"""

from .envelope import (
    ENVELOPE_SEPARATOR,
    MessageError,
    compose_envelope,
    new_msg_id,
    split_thread_key,
    thread_key,
)
from .ledger import Ledger, messages_root

__all__ = [
    "ENVELOPE_SEPARATOR",
    "Ledger",
    "MessageError",
    "compose_envelope",
    "messages_root",
    "new_msg_id",
    "split_thread_key",
    "thread_key",
]
