"""Firestore adapter for one-document atomic state; not wired to the runtime.

No Firebase client is constructed here. The caller must explicitly supply an
authorized client after deployment review. Transaction callbacks must be pure.
"""

import json
import re
from copy import deepcopy

from google.api_core import exceptions as google_exceptions
from google.cloud import firestore

_ID = re.compile(r"^[A-Za-z0-9._~-]{1,128}$")
_MAX_BYTES = 700_000


class CommitOutcomeUnknown(RuntimeError):
    """The transaction could have committed; do not perform an external action."""


class FirestoreAtomicStateStore:
    """Serializable state transform bounded to one Firestore document."""

    def __init__(self, client, *, max_attempts=5):
        if not callable(getattr(client, "transaction", None)) or not callable(
            getattr(client, "collection", None)
        ):
            raise TypeError("injected Firestore client required")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
            raise ValueError("bounded transaction retries required")
        self._client, self._max_attempts = client, max_attempts

    def transact(self, key, operation):
        if (not isinstance(key, str) or not _ID.fullmatch(key) or key in {".", ".."}
                or (key.startswith("__") and key.endswith("__")) or not callable(operation)):
            raise ValueError("valid state key and operation required")
        reference = self._client.collection("_ts_d5_coordination").document(key)
        transaction = self._client.transaction(max_attempts=self._max_attempts)
        callback_completed = [False]

        def transform(tx):
            callback_completed[0] = False
            snapshot = next(iter(tx.get(reference)))
            current = snapshot.to_dict() if snapshot.exists else {}
            if not isinstance(current, dict):
                raise ValueError("coordination state must be a mapping")
            updated, result = operation(deepcopy(current))
            if not isinstance(updated, dict):
                raise ValueError("coordination state must be a mapping")
            try:
                encoded = json.dumps(updated, ensure_ascii=False, allow_nan=False,
                                     separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError, UnicodeError):
                raise ValueError("coordination state must be finite JSON") from None
            if len(encoded) > _MAX_BYTES:
                raise ValueError("coordination state exceeds document budget")
            tx.set(reference, updated)
            callback_completed[0] = True
            return result

        try:
            return firestore.transactional(transform)(transaction)
        except (google_exceptions.DeadlineExceeded, google_exceptions.ServiceUnavailable,
                google_exceptions.InternalServerError, google_exceptions.Unknown) as exc:
            if callback_completed[0]:
                raise CommitOutcomeUnknown("coordination commit outcome unknown") from exc
            raise
