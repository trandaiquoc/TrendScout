"""Fail-closed future LIVE gate and offline tranche-aware runtime adapter.

The production gate deliberately has no enable operation or transport factory.
The fixture runner can only be constructed over SYNTHETIC state and MockTransport.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import httpx

from app.jobs.public_admission import AdmissionDenied, PublicAdmission
from app.local_ledger import STATE_KEY
from app.sources.catalog import WIKIMEDIA_SOURCE_ID
from app.sources.live_gateway import checked_source_approval
from app.storage.d6_authorization_tranches import SameLedgerTranches
from app.workers.guarded_attempt import GuardedAttemptRunner


class FutureWikimediaLiveActivation:
    """Visible future activation gate; its default and only current state is BLOCKED."""

    status = "BLOCKED"

    def require_live_dispatch(self):
        raise AdmissionDenied("LIVE_ACTIVATION_BLOCKED")


class SameLedgerRuntimeAdmissionStore:
    """Expose PublicAdmission state inside schema v2 at the existing state key."""

    def __init__(self, store):
        if (getattr(store, "data_mode", None) != "SYNTHETIC"
                or not callable(getattr(store, "transact", None))):
            raise ValueError("offline activation requires a synthetic atomic store")
        self.store = store
        self.data_mode = "SYNTHETIC"

    def transact(self, key, operation):
        if key != STATE_KEY or not callable(operation):
            raise ValueError("offline runtime state must use the existing ledger key")

        def mutate(envelope):
            SameLedgerTranches._validate_v2(envelope)
            runtime = envelope.get("runtime_admission_state", {})
            updated_runtime, result = operation(runtime)
            updated = dict(envelope)
            updated["runtime_admission_state"] = updated_runtime
            SameLedgerTranches._validate_v2(updated)
            return updated, result

        return self.store.transact(STATE_KEY, mutate)


class TrancheAwareGuardedAttemptRunner(GuardedAttemptRunner):
    """Conservative two-store handoff: tranche first, runtime receipt second."""

    def __init__(self, admission: PublicAdmission, tranches: SameLedgerTranches, *,
                 authorization_id: str, approvals, endpoint_scope: str,
                 country_scope: str, access_scope: str, observation_date: str,
                 clock=lambda: datetime.now(timezone.utc)):
        super().__init__(admission, clock=clock)
        if not isinstance(tranches, SameLedgerTranches):
            raise TypeError("validated tranche service required")
        if getattr(tranches.store, "data_mode", None) != "SYNTHETIC":
            raise ValueError("tranche-aware offline runner requires synthetic state")
        self.tranches = tranches
        self.authorization_id = authorization_id
        self.approvals = approvals
        self.endpoint_scope = endpoint_scope
        self.country_scope = country_scope
        self.access_scope = access_scope
        self.observation_date = observation_date

    def _authorization(self):
        state = self.tranches.store.transact(
            STATE_KEY, lambda current: (current, current)
        )
        SameLedgerTranches._validate_v2(state)
        matches = [row for row in state["authorizations"]
                   if row.get("authorization_id") == self.authorization_id]
        if len(matches) != 1:
            raise AdmissionDenied("AUTHORIZATION_MISSING")
        return matches[0]

    async def run(self, *, job_id, owner, fence, source_id, attempt_id,
                  timeout_seconds, request, before_settle=None):
        from app.workers.guarded_attempt import AttemptOutcome

        if source_id != WIKIMEDIA_SOURCE_ID:
            return AttemptOutcome("SOURCE_DISABLED")
        try:
            auth = self._authorization()
        except AdmissionDenied as exc:
            return AttemptOutcome(exc.code)
        approval = checked_source_approval(
            self.approvals, source_id, self.admission.policy.plan_fingerprint,
            self.clock, runner=self,
        )
        if approval.approval_id != auth["approval_id"]:
            return AttemptOutcome("APPROVAL_IDENTITY_MISMATCH")
        if approval.policy_version != auth["policy_version"]:
            return AttemptOutcome("AUTHORIZATION_POLICY_MISMATCH")
        if (auth["query_plan_fingerprint"] != self.admission.policy.plan_fingerprint
                or auth["endpoint_scope"] != self.endpoint_scope
                or auth["country_scope"] != self.country_scope
                or auth["access_scope"] != self.access_scope
                or auth["observation_date"] != self.observation_date):
            return AttemptOutcome("AUTHORIZATION_SCOPE_MISMATCH")
        now = self.clock()
        try:
            reserved = self.tranches.reserve_attempt(
                authorization_id=self.authorization_id,
                approval_id=approval.approval_id,
                source_id=source_id,
                policy_version=auth["policy_version"],
                policy_digest=self.admission.policy_digest,
                query_plan_fingerprint=self.admission.policy.plan_fingerprint,
                endpoint_scope=self.endpoint_scope,
                country_scope=self.country_scope,
                access_scope=self.access_scope,
                observation_date=self.observation_date,
                job_id=job_id,
                attempt_id=attempt_id,
                idempotency_key=f"d6266:{self.authorization_id}:{attempt_id}",
                cost_units=1,
                monetary_fee="0",
                currency=auth["currency"],
                now_utc=now.astimezone(timezone.utc).isoformat(),
            )
        except AdmissionDenied as exc:
            return AttemptOutcome(exc.code)
        if reserved != "NEW":
            return AttemptOutcome("ALREADY_RESERVED")

        # A crash, cancellation or uncertain reservation handoff consumes the
        # tranche slot. Only a definite terminal PublicAdmission result settles it.
        outcome = await super().run(
            job_id=job_id, owner=owner, fence=fence, source_id=source_id,
            attempt_id=attempt_id, timeout_seconds=timeout_seconds,
            request=request, before_settle=before_settle,
        )
        if outcome.status == "COMPLETED" and not outcome.settlement_pending:
            self._settle(attempt_id, "RUNTIME_COMPLETED", self.clock())
        return outcome

    def _settle(self, attempt_id, outcome, now):
        reservation_id = hashlib.sha256(
            f"{self.authorization_id}:{attempt_id}".encode()
        ).hexdigest()
        return self.tranches.settle_attempt(
            reservation_id=reservation_id, outcome=outcome,
            now_utc=now.astimezone(timezone.utc).isoformat(),
        )

    def settle_recovered_attempt(self, job_id, owner, fence, *, attempt_id, now):
        """Settle both ledgers after a durable Wikimedia page receipt is replayed."""
        result = self.admission.settle_attempt(
            job_id, owner, fence, attempt_id=attempt_id, now=now,
        )
        self._settle(attempt_id, "RECOVERED_PAGE_RECEIPT", now)
        return result


def require_offline_activation(transport, tranches):
    """Construction guard shared by the offline composition and its acceptance tests."""
    if not isinstance(transport, httpx.MockTransport):
        raise ValueError("offline Wikimedia activation requires MockTransport")
    if (not isinstance(tranches, SameLedgerTranches)
            or getattr(tranches.store, "data_mode", None) != "SYNTHETIC"):
        raise ValueError("offline Wikimedia activation requires synthetic tranche state")
    return True
