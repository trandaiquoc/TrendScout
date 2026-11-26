"""Reject obvious credential-bearing fields before persistent storage."""

_FORBIDDEN = frozenset({
    "api_key", "apikey", "access_token", "authorization", "password", "secret", "credential", "token",
})


def validate_no_credentials(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("document has an invalid field name")
            normalized = key.casefold().replace("-", "_")
            if (normalized in _FORBIDDEN
                    or normalized.endswith(("_api_key", "_access_token", "_password", "_secret"))):
                raise ValueError("document contains a credential-shaped field")
            validate_no_credentials(child)
    elif isinstance(value, list):
        for child in value:
            validate_no_credentials(child)
