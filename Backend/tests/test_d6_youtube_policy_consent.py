"""Fail-closed API checks for YouTube policy publication and browser consent."""

import unittest

from fastapi import HTTPException
from starlette.requests import Request

from app.api.fetches import _require_youtube_policy_consent
from app.google_live_settings import GoogleLiveSettings
from app.sources.youtube.policy import (
    YOUTUBE_POLICY_VERSION,
    YOUTUBE_POLICY_VERSION_HEADER,
)


def request_with_policy_version(value=None):
    headers = []
    if value is not None:
        headers.append((YOUTUBE_POLICY_VERSION_HEADER.lower().encode(), value.encode()))
    return Request({
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/fetches",
        "query_string": b"",
        "headers": headers,
        "server": ("test", 80),
        "client": ("test", 1234),
    })


class DistributedFacade:
    def __init__(self, policy_status):
        self.youtube_capabilities = {
            "policy_status": policy_status,
            "policy_version": YOUTUBE_POLICY_VERSION,
        }


class YouTubePolicyConsentTests(unittest.TestCase):
    def test_live_runtime_requires_separate_live_and_policy_approval(self):
        live_only = GoogleLiveSettings.from_environment({
            "TREND_SCOUT_YOUTUBE_LIVE_APPROVED": "true",
        })
        both_approved = GoogleLiveSettings.from_environment({
            "TREND_SCOUT_YOUTUBE_LIVE_APPROVED": "true",
            "TREND_SCOUT_YOUTUBE_POLICY_APPROVED": "true",
        })

        self.assertFalse(live_only.youtube_approval_configured)
        self.assertFalse(live_only.youtube_policy_approved)
        self.assertTrue(both_approved.youtube_approval_configured)

    def test_unpublished_policy_blocks_youtube_fetch(self):
        with self.assertRaises(HTTPException) as raised:
            _require_youtube_policy_consent(
                request_with_policy_version(YOUTUBE_POLICY_VERSION),
                DistributedFacade("DRAFT"),
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "YOUTUBE_POLICY_NOT_APPROVED")

    def test_published_policy_requires_explicit_current_version_consent(self):
        with self.assertRaises(HTTPException) as raised:
            _require_youtube_policy_consent(
                request_with_policy_version(), DistributedFacade("APPROVED"))

        self.assertEqual(raised.exception.status_code, 428)
        self.assertEqual(raised.exception.detail["code"], "YOUTUBE_POLICY_CONSENT_REQUIRED")

    def test_exact_current_policy_version_allows_youtube_fetch(self):
        self.assertIsNone(_require_youtube_policy_consent(
            request_with_policy_version(YOUTUBE_POLICY_VERSION),
            DistributedFacade("APPROVED"),
        ))

    def test_stale_policy_version_does_not_count_as_consent(self):
        with self.assertRaises(HTTPException) as raised:
            _require_youtube_policy_consent(
                request_with_policy_version("d6-youtube-old"),
                DistributedFacade("APPROVED"),
            )

        self.assertEqual(raised.exception.status_code, 428)
        self.assertEqual(raised.exception.detail["code"], "YOUTUBE_POLICY_CONSENT_REQUIRED")


if __name__ == "__main__":
    unittest.main()
