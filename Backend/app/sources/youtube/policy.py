"""Policy version and request contract for YouTube API Client consent."""

YOUTUBE_POLICY_VERSION = "d6-youtube-v2"
YOUTUBE_POLICY_VERSION_HEADER = "X-YouTube-Policy-Version"
# The owner reviewed and approved the published Privacy Policy and Terms on
# 2026-11-16. A future version bump fails closed until its publication is approved.
YOUTUBE_POLICY_APPROVED_VERSION = "d6-youtube-v2"
YOUTUBE_POLICY_APPROVED = YOUTUBE_POLICY_APPROVED_VERSION == YOUTUBE_POLICY_VERSION
