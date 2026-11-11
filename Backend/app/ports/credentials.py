"""Backend-only provider credential boundary.

Implementations belong to the deployment composition layer. A reference names a
credential; it is never a secret or authorization granted to a public user.
"""

from typing import Protocol


class CredentialProvider(Protocol):
    def resolve(self, reference: str) -> str | None:
        """Resolve a server-owned reference, or return None when unavailable."""
        ...
