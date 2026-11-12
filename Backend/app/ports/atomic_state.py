"""Serializable, durable transaction boundary for shared coordination state.

Callbacks may be invoked more than once and must be deterministic and free of
I/O or other side effects. A successful return means the state was committed.
On an ambiguous commit, callers must not perform the guarded external action.
"""

from typing import Callable, Protocol, TypeVar

T = TypeVar("T")
State = dict[str, object]


class AtomicStateStore(Protocol):
    def transact(self, key: str, operation: Callable[[State], tuple[State, T]]) -> T:
        """Atomically read, transform and replace one state record."""
        ...
