"""Server-side Vercel credential access with an explicit provider-key allowlist."""

from __future__ import annotations

import os


class VercelEnvironmentCredentialProvider:
    """Resolve only provider keys approved for the Vercel runtime."""

    _ALLOWED_REFERENCES = frozenset({"SERPAPI_API_KEY", "YOUTUBE_API_KEY"})

    def resolve(self, reference):
        if reference not in self._ALLOWED_REFERENCES:
            return None
        return os.environ.get(reference)
