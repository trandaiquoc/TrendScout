"""A bounded offline quota pool; distributed admission remains a D5 requirement."""

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class QuotaSnapshot:
    max_requests: int
    max_cost_units: int
    attempted_requests: int
    reserved_cost_units: int


class SharedQuotaPool:
    """Reserve before each attempt across engines, including retries.

    This is deliberately process-local and cannot protect a public deployment.
    Cost units are conservative internal reservations, not a provider invoice.
    """

    def __init__(self, max_requests: int, max_cost_units: int):
        if type(max_requests) is not int or not 1 <= max_requests <= 500:
            raise ValueError("max_requests must be within 1..500")
        if type(max_cost_units) is not int or not 1 <= max_cost_units <= 500:
            raise ValueError("max_cost_units must be within 1..500")
        self._max_requests = max_requests
        self._max_cost_units = max_cost_units
        self._attempted = 0
        self._cost = 0
        self._lock = Lock()

    def reserve(self) -> bool:
        with self._lock:
            if self._attempted >= self._max_requests or self._cost >= self._max_cost_units:
                return False
            self._attempted += 1
            self._cost += 1
            return True

    def snapshot(self) -> QuotaSnapshot:
        with self._lock:
            return QuotaSnapshot(
                self._max_requests, self._max_cost_units, self._attempted, self._cost
            )
