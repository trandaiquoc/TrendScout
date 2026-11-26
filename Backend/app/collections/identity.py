"""Stable collection and suppression identity helpers."""

import hashlib


def fingerprint(value: str) -> str:
    return hashlib.sha256(" ".join(value.casefold().split()).encode()).hexdigest()
