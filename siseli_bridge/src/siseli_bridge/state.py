import threading
from typing import Dict, Optional

# Shared mutable state consumed by both mqtt.py and parsers.py.
# Having these globals here breaks the circular import that previously required
# a deferred-import hack (_get_mqtt_globals) inside parsers.py.

LAST_STATE: Dict[str, object] = {}
DISCOVERY_PUBLISHED: bool = False
PUBLISHED_SENSOR_KEYS: set = set()

# --- Thread safety -----------------------------------------------------------
# LAST_STATE is written from multiple threads: the scapy sniffer (via parsers),
# each proxy socket handler, the ThreadingHTTPServer workers, the poller, and
# paho's network thread.  A single lock serialises all access; callers that
# need a consistent view should use get_state_snapshot() which returns a copy.
STATE_LOCK = threading.RLock()

# Global shutdown flag.  Replaces the old by-value ``RUNNING`` boolean that
# lived in mqtt.py — importing it by value meant ``shutdown()`` in core.py only
# rebind its own local copy, so loops in other modules never observed the
# change.  Using a threading.Event makes the flag shared and waitable.
RUNNING_EVENT = threading.Event()
RUNNING_EVENT.set()  # started as "running"


def get_state_snapshot() -> Dict[str, object]:
    """Return a point-in-time copy of LAST_STATE (thread-safe)."""
    with STATE_LOCK:
        return dict(LAST_STATE)


def update_state(new_values: Dict[str, object]) -> None:
    """Merge *new_values* into LAST_STATE (thread-safe)."""
    with STATE_LOCK:
        LAST_STATE.update(new_values)


def apply_and_snapshot(new_values: Dict[str, object]):
    """Atomically capture the prior state, merge *new_values*, and snapshot.

    Returns ``(previous, updated)`` -- copies of LAST_STATE before and after
    the merge. Read, update, and post-snapshot happen under one lock hold, so
    no other thread interleaves between them: the logged diff and the state
    published correspond to the same merge.
    """
    with STATE_LOCK:
        previous = dict(LAST_STATE)
        LAST_STATE.update(new_values)
        updated = dict(LAST_STATE)
        return previous, updated


def get_state_value(key: str, default: Optional[object] = None) -> object:
    """Read a single key from LAST_STATE (thread-safe)."""
    with STATE_LOCK:
        return LAST_STATE.get(key, default)


def has_any_state() -> bool:
    """Return True if LAST_STATE has at least one non-None value."""
    with STATE_LOCK:
        return any(v is not None for v in LAST_STATE.values())
