"""Strict OpenRouter adapter for currently verified zero-price endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
import re
import math
from urllib.parse import quote

import httpx


API_BASE = "https://openrouter.ai/api/v1"
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,90}/[a-z0-9][a-z0-9_.:-]{0,150}:free$")
_BILLABLE_PRICING_FIELDS = frozenset({"prompt", "completion"})


class OpenRouterError(RuntimeError):
    """Sanitized adapter failure. Provider response text is never retained."""

    def __init__(self, code: str, *, retry_at: str | None = None, usage=None):
        safe = code if code in {
            "AI_KEY_MISSING", "AI_MODEL_UNAVAILABLE", "AI_PRICING_UNVERIFIED",
            "AI_POLICY_DENIED", "AI_RATE_LIMITED", "AI_PROVIDER_UNAVAILABLE",
            "AI_INVALID_OUTPUT",
        } else "AI_PROVIDER_UNAVAILABLE"
        super().__init__(safe)
        self.code = safe
        self.retry_at = retry_at
        self.usage = usage if isinstance(usage, dict) else None


@dataclass(frozen=True)
class VerifiedModel:
    model_id: str
    provider_name: str
    provider_tag: str
    context_tokens: int
    max_output_tokens: int
    supports_json_schema: bool


def _number(value) -> Decimal | None:
    if type(value) not in (str, int, float):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _all_zero_pricing(value) -> bool:
    if not isinstance(value, dict) or not _BILLABLE_PRICING_FIELDS <= set(value):
        return False
    for amount in value.values():
        number = _number(amount)
        if number is None or number != 0:
            return False
    return True


def _retry_at(headers, now=None) -> str | None:
    now = now or datetime.now(timezone.utc)
    retry_after = headers.get("Retry-After")
    raw = retry_after or headers.get("X-RateLimit-Reset")
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    try:
        seconds = float(raw)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                return None
            parsed = parsed.astimezone(timezone.utc)
            if parsed < now or parsed > now + timedelta(days=7):
                return None
            return parsed.isoformat()
        except (TypeError, ValueError, OverflowError):
            return None
    if not math.isfinite(seconds):
        return None
    if retry_after is None and seconds > 1_000_000_000:
        if seconds < now.timestamp() or seconds > now.timestamp() + 7 * 24 * 60 * 60:
            return None
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    if seconds < 0 or seconds > 7 * 24 * 60 * 60:
        return None
    return datetime.fromtimestamp(now.timestamp() + seconds, timezone.utc).isoformat()


class OpenRouterAdapter:
    """Provider boundary with exact-model, price, JSON, and ZDR checks."""

    def __init__(self, api_key: str | None, *, client: httpx.Client | None = None):
        self._api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))

    def close(self):
        if self._owns_client:
            self._client.close()

    def _request(self, method: str, path: str, *, authenticated=False, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        if authenticated:
            if not self._api_key:
                raise OpenRouterError("AI_KEY_MISSING")
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = self._client.request(
                method, API_BASE + path, headers=headers, follow_redirects=False, **kwargs
            )
        except (httpx.TimeoutException, httpx.NetworkError, httpx.ProtocolError):
            raise OpenRouterError("AI_PROVIDER_UNAVAILABLE") from None
        if response.status_code == 429:
            raise OpenRouterError("AI_RATE_LIMITED", retry_at=_retry_at(response.headers))
        if response.status_code in (401, 403) and authenticated:
            raise OpenRouterError("AI_KEY_MISSING") from None
        if response.status_code < 200 or response.status_code >= 300:
            raise OpenRouterError("AI_PROVIDER_UNAVAILABLE") from None
        try:
            payload = response.json()
        except (ValueError, TypeError):
            raise OpenRouterError("AI_PROVIDER_UNAVAILABLE") from None
        if not isinstance(payload, dict):
            raise OpenRouterError("AI_PROVIDER_UNAVAILABLE")
        return payload, response

    def verify_model(self, model_id: str) -> VerifiedModel:
        if not isinstance(model_id, str) or not _MODEL_ID.fullmatch(model_id):
            raise OpenRouterError("AI_MODEL_UNAVAILABLE")
        encoded_id = quote(model_id, safe="/-._")
        catalog, _ = self._request("GET", "/models")
        models = catalog.get("data")
        if not isinstance(models, list):
            raise OpenRouterError("AI_MODEL_UNAVAILABLE")
        model = next((entry for entry in models
                      if isinstance(entry, dict) and entry.get("id") == model_id), None)
        if model is None:
            raise OpenRouterError("AI_MODEL_UNAVAILABLE")
        if not _all_zero_pricing(model.get("pricing")):
            raise OpenRouterError("AI_PRICING_UNVERIFIED")

        endpoint_payload, _ = self._request("GET", f"/models/{encoded_id}/endpoints")
        endpoint_data = endpoint_payload.get("data")
        endpoints = endpoint_data.get("endpoints") if isinstance(endpoint_data, dict) else None
        if not isinstance(endpoints, list) or not endpoints:
            raise OpenRouterError("AI_MODEL_UNAVAILABLE")

        zdr_payload, _ = self._request("GET", "/endpoints/zdr")
        zdr_endpoints = zdr_payload.get("data")
        if not isinstance(zdr_endpoints, list):
            raise OpenRouterError("AI_POLICY_DENIED")

        base_model_id = model_id.removesuffix(":free")
        active_endpoint = False
        pricing_unverified = False
        policy_denied = False
        for endpoint in endpoints:
            if not isinstance(endpoint, dict):
                continue
            if type(endpoint.get("status")) is not int or endpoint.get("status") != 0:
                continue
            active_endpoint = True
            provider_name = endpoint.get("provider_name")
            provider_tag = endpoint.get("tag")
            parameters = endpoint.get("supported_parameters")
            context_tokens = endpoint.get("context_length")
            max_output_tokens = endpoint.get("max_completion_tokens")
            json_supported = isinstance(parameters, list) and "structured_outputs" in parameters
            if not _all_zero_pricing(endpoint.get("pricing")):
                pricing_unverified = True
                continue
            if (not isinstance(provider_name, str) or not provider_name.strip()
                    or not isinstance(provider_tag, str) or not provider_tag.strip()
                    or not json_supported or type(context_tokens) is not int or context_tokens <= 0
                    or type(max_output_tokens) is not int or max_output_tokens <= 0):
                continue
            policy_match = next((entry for entry in zdr_endpoints
                if isinstance(entry, dict)
                and entry.get("model_id") == base_model_id
                and entry.get("provider_name") == provider_name
                and entry.get("tag") == provider_tag
                and entry.get("safety_retention") is False), None)
            if policy_match is not None:
                return VerifiedModel(
                    model_id=model_id,
                    provider_name=provider_name,
                    provider_tag=provider_tag,
                    context_tokens=context_tokens,
                    max_output_tokens=max_output_tokens,
                    supports_json_schema=("structured_outputs" in parameters),
                )
            policy_denied = True
        if pricing_unverified:
            raise OpenRouterError("AI_PRICING_UNVERIFIED")
        if policy_denied or active_endpoint:
            raise OpenRouterError("AI_POLICY_DENIED")
        raise OpenRouterError("AI_MODEL_UNAVAILABLE")

    def complete(self, verified: VerifiedModel, *, messages: list[dict], schema: dict,
                 max_tokens: int) -> tuple[str, dict | None, str | None]:
        if self._api_key is None:
            raise OpenRouterError("AI_KEY_MISSING")
        if (not isinstance(verified, VerifiedModel) or max_tokens < 1
                or max_tokens > verified.max_output_tokens):
            raise OpenRouterError("AI_MODEL_UNAVAILABLE")
        body = {
            "model": verified.model_id,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "trendscout_ai_output", "strict": True, "schema": schema},
            },
            "provider": {
                "only": [verified.provider_tag],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "zdr": True,
                "max_price": {"prompt": 0, "completion": 0},
            },
        }
        payload, response = self._request(
            "POST", "/chat/completions", authenticated=True, json=body,
            headers={"Content-Type": "application/json"},
        )
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            usage = None
        elif any(type(usage.get(name)) is not int or usage[name] < 0
                 for name in ("prompt_tokens", "completion_tokens")):
            usage = None
        else:
            usage = {"input_tokens": usage["prompt_tokens"],
                     "output_tokens": usage["completion_tokens"]}
        try:
            choice = payload["choices"][0]
            message = choice["message"]
            content = message.get("content")
            if choice.get("finish_reason") == "length" or message.get("refusal"):
                raise ValueError
            if not isinstance(content, str):
                raise ValueError
        except (KeyError, IndexError, TypeError, ValueError):
            raise OpenRouterError("AI_INVALID_OUTPUT", retry_at=_retry_at(response.headers),
                                  usage=usage) from None
        return content, usage, _retry_at(response.headers)

    def key_quota_metadata(self) -> dict | None:
        """Return only provider quota counters, excluding labels and key metadata."""
        if self._api_key is None:
            return None
        try:
            payload, _ = self._request("GET", "/key", authenticated=True)
        except OpenRouterError:
            return None
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        result = {}
        for field in ("is_free_tier", "limit_reset"):
            value = data.get(field)
            if (field == "is_free_tier" and type(value) is bool
                    or field == "limit_reset" and isinstance(value, str) and len(value) <= 32):
                result[field] = value
        for field in ("limit", "limit_remaining", "usage", "usage_daily", "usage_weekly", "usage_monthly"):
            value = _number(data.get(field))
            if value is not None and 0 <= value <= Decimal("1e18"):
                result[field] = float(value)
        return result or None
