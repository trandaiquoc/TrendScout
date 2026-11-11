"""Bounded errors that never contain provider bodies, URLs, or credentials."""


class SerpApiError(Exception):
    def __init__(self, code: str, *, status: int | None = None,
                 retryable: bool = False, retry_after: float | None = None):
        self.code = code
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after
        super().__init__(f"SerpApi request failed: {code}")

    def __repr__(self) -> str:
        return f"SerpApiError(code={self.code!r}, status={self.status!r})"
