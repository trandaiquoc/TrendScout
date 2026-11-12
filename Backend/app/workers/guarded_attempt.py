"""Run one fixed-source attempt only after durable, fenced budget reservation.

This worker primitive is not connected to public endpoints or live adapters.
Its timeout guarantee requires a cancellation-cooperative transport.
"""

import asyncio
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.jobs.public_admission import AdmissionDenied, PublicAdmission


@dataclass(frozen=True)
class AttemptOutcome:
    status: str
    value: Any = field(default=None, repr=False)
    settlement_pending: bool = False


class GuardedAttemptRunner:
    def __init__(self, admission: PublicAdmission, *, clock=lambda: datetime.now(timezone.utc)):
        if not isinstance(admission, PublicAdmission) or not callable(clock):
            raise TypeError("validated admission and server clock required")
        self.admission, self.clock = admission, clock

    async def run(self, *, job_id: str, owner: str, fence: int, source_id: str,
                  attempt_id: str, timeout_seconds: float,
                  request: Callable[[], Awaitable[Any]]) -> AttemptOutcome:
        if source_id not in self.admission.policy.enabled_sources or not callable(request):
            raise ValueError("server-approved source and request factory required")
        maximum = self.admission.max_attempt_seconds(source_id)
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= maximum):
            raise ValueError("request timeout must fit the reserved in-flight slot")
        # A reservation RPC that raises or is cancelled may have committed. Never
        # create the request coroutine unless a definite NEW acknowledgement arrives.
        result = await asyncio.to_thread(
            self.admission.reserve_attempt, job_id, owner, fence, source_id=source_id,
            attempt_id=attempt_id, now=self.clock(),
        )
        if result != "NEW":
            return AttemptOutcome("ALREADY_RESERVED")
        try:
            deadline = await asyncio.to_thread(
                self.admission.check_attempt, job_id, owner, fence,
                source_id=source_id, attempt_id=attempt_id, now=self.clock(),
            )
        except AdmissionDenied:
            # No request was created; reservation remains conservatively charged.
            return AttemptOutcome("ADMISSION_REVOKED", settlement_pending=True)
        remaining = (deadline - self.clock()).total_seconds()
        if remaining <= 0:
            return AttemptOutcome("ADMISSION_REVOKED", settlement_pending=True)
        try:
            value = await asyncio.wait_for(request(), timeout=min(timeout_seconds, remaining))
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            # An upstream timeout has an uncertain outcome; the slot expires later.
            return AttemptOutcome("UPSTREAM_TIMEOUT", settlement_pending=True)
        except Exception:
            # Do not propagate provider exception text, URLs or credentials.
            return AttemptOutcome("UPSTREAM_ERROR", settlement_pending=True)
        try:
            await asyncio.to_thread(self.admission.settle_attempt, job_id, owner, fence,
                                    attempt_id=attempt_id, now=self.clock())
        except Exception:
            return AttemptOutcome("COMPLETED", value, settlement_pending=True)
        return AttemptOutcome("COMPLETED", value)
