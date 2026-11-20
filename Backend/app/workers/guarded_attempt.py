"""Run one fixed-source attempt only after durable, fenced budget reservation.

This worker primitive is not connected to public endpoints or live adapters.
Its timeout guarantee requires a cancellation-cooperative transport.
"""

import asyncio
import inspect
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from app.jobs.public_admission import AdmissionDenied, PublicAdmission

_SAFE_EXCEPTION_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


@dataclass(frozen=True)
class AttemptOutcome:
    status: str
    value: Any = field(default=None, repr=False)
    settlement_pending: bool = False
    failure_phase: str | None = None
    failure_reason: str | None = None
    exception_class: str | None = None


def _safe_exception_class(error: Exception) -> str:
    name = type(error).__name__
    return name if _SAFE_EXCEPTION_CLASS.fullmatch(name) else "UnknownError"


class GuardedAttemptRunner:
    def __init__(self, admission: PublicAdmission, *, clock=lambda: datetime.now(timezone.utc)):
        if not isinstance(admission, PublicAdmission) or not callable(clock):
            raise TypeError("validated admission and server clock required")
        self.admission, self.clock = admission, clock

    async def run(self, *, job_id: str, owner: str, fence: int, source_id: str,
                  attempt_id: str, timeout_seconds: float,
                  request: Callable[[], Awaitable[Any]],
                  before_settle: Callable[[Any], Any] | None = None,
                  pool_name: str | None = None) -> AttemptOutcome:
        if source_id not in self.admission.policy.enabled_sources or not callable(request):
            raise ValueError("server-approved source and request factory required")
        if before_settle is not None and not callable(before_settle):
            raise ValueError("result receipt callback must be callable")
        maximum = (self.admission.max_attempt_seconds(source_id)
                   if pool_name is None else self.admission.max_attempt_seconds(source_id, pool_name))
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
                or not 0 < timeout_seconds <= maximum):
            raise ValueError("request timeout must fit the reserved in-flight slot")
        # A reservation RPC that raises or is cancelled may have committed. Never
        # create the request coroutine unless a definite NEW acknowledgement arrives.
        reserve_kwargs = {"source_id": source_id, "attempt_id": attempt_id, "now": self.clock()}
        if pool_name is not None:
            reserve_kwargs["pool_name"] = pool_name
        result = await asyncio.to_thread(
            self.admission.reserve_attempt, job_id, owner, fence, **reserve_kwargs,
        )
        if result != "NEW":
            return AttemptOutcome("ALREADY_RESERVED")
        try:
            check_kwargs = {"source_id": source_id, "attempt_id": attempt_id,
                            "now": self.clock()}
            if pool_name is not None:
                check_kwargs["pool_name"] = pool_name
            deadline = await asyncio.to_thread(
                self.admission.check_attempt, job_id, owner, fence, **check_kwargs,
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
        if before_settle is not None:
            try:
                persist_remaining = (deadline - self.clock()).total_seconds()
                if persist_remaining <= 0:
                    return AttemptOutcome(
                        "RESULT_PERSISTENCE_FAILED", settlement_pending=True,
                        failure_phase="RESULT_PERSISTENCE",
                        failure_reason="DEADLINE_EXPIRED",
                    )
                result = await asyncio.wait_for(
                    asyncio.to_thread(before_settle, value), timeout=persist_remaining
                )
                if inspect.isawaitable(result):
                    persist_remaining = (deadline - self.clock()).total_seconds()
                    if persist_remaining <= 0:
                        return AttemptOutcome(
                            "RESULT_PERSISTENCE_FAILED", settlement_pending=True,
                            failure_phase="RESULT_PERSISTENCE",
                            failure_reason="DEADLINE_EXPIRED",
                        )
                    await asyncio.wait_for(result, timeout=persist_remaining)
            except TimeoutError as exc:
                return AttemptOutcome(
                    "RESULT_PERSISTENCE_FAILED", settlement_pending=True,
                    failure_phase="RESULT_PERSISTENCE",
                    failure_reason="CALLBACK_TIMEOUT",
                    exception_class=_safe_exception_class(exc),
                )
            except Exception as exc:
                # No settlement acknowledgement is allowed until the bounded
                # result receipt has definitely crossed its durable boundary.
                return AttemptOutcome(
                    "RESULT_PERSISTENCE_FAILED", settlement_pending=True,
                    failure_phase="RESULT_PERSISTENCE",
                    failure_reason="CALLBACK_ERROR",
                    exception_class=_safe_exception_class(exc),
                )
        try:
            await asyncio.to_thread(self.admission.settle_attempt, job_id, owner, fence,
                                    attempt_id=attempt_id, now=self.clock())
        except Exception:
            return AttemptOutcome("COMPLETED", value, settlement_pending=True)
        return AttemptOutcome("COMPLETED", value)
