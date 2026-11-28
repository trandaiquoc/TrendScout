"""Strict OpenRouter adapter for currently verified zero-price endpoints."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from threading import local
from urllib.parse import quote

import httpx

API_BASE = "https://openrouter.ai/api/v1"
STRICT_PRIVATE = "STRICT_PRIVATE"
PUBLIC_DEMO = "PUBLIC_DEMO"
PUBLIC_DATA = "PUBLIC_DATA"
_PUBLIC_DEMO_MODEL_ENDPOINTS = frozenset({
    ("particle-ai/particle-1:free", "Particle AI", "particle-ai/us"),
})
_PUBLIC_DATA_PROVIDER_ENDPOINTS = frozenset({("Particle AI", "particle-ai/us")})
_REQUIRED_STRUCTURED_PARAMETERS = frozenset({
    "temperature", "max_tokens", "response_format", "structured_outputs",
})
_MODEL_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,90}/[a-z0-9][a-z0-9_.:-]{0,150}:free$")
_BILLABLE_PRICING_FIELDS = frozenset({"prompt", "completion"})
_SAFE_DIAGNOSTIC_TEXT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _./:-]{0,79}$")
_SAFE_PROVIDER_SLUG = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,79}$")
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")
_SAFE_LIMIT_SOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_SAFE_SCHEMA_PATH = re.compile(r"^[A-Za-z0-9_.$\[\]-]{1,160}$")
_SAFE_GENERATION_ID = re.compile(r"^gen-[0-9A-Za-z-]{1,124}$")
_SAFE_MODEL_METADATA = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,249}$")
_ERROR_BODY_LIMIT = 8192


def _safe_diagnostic_text(value, pattern=_SAFE_DIAGNOSTIC_TEXT):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if (not value or not value.isascii() or not pattern.fullmatch(value)
            or any(marker in value.casefold() for marker in (
                "sk-", "bearer ", "http://", "https://", "api_key", "prompt",
                "secret", "token", "authorization", "password", "cookie",
            ))):
        return None
    return value


def sanitize_provider_diagnostics(value):
    """Keep only small, non-content routing facts suitable for a safe result."""
    if not isinstance(value, dict):
        return None
    result = {}
    status = value.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    error_code = value.get("error_code")
    if type(error_code) is int and 0 <= error_code <= 9999:
        result["error_code"] = error_code
    elif isinstance(error_code, str):
        safe = _safe_diagnostic_text(error_code, re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,39}$"))
        if safe:
            result["error_code"] = safe
    error_type = _safe_diagnostic_text(
        value.get("error_type"), re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"),
    )
    if error_type:
        result["error_type"] = error_type
    error_category = value.get("error_category")
    if error_category in {
        "STRUCTURED_OUTPUT_SCHEMA_REJECTED", "UNSUPPORTED_REQUEST_PARAMETER",
        "RATE_LIMIT", "PROVIDER_ERROR",
    }:
        result["error_category"] = error_category
    schema_keyword = value.get("schema_keyword")
    if schema_keyword in {
        "additionalProperties", "enum", "items", "maxItems", "maxLength",
        "minItems", "minLength", "required", "type", "uniqueItems",
    }:
        result["schema_keyword"] = schema_keyword
    parameter_path = value.get("parameter_path")
    if isinstance(parameter_path, str) and _SAFE_SCHEMA_PATH.fullmatch(parameter_path):
        result["parameter_path"] = parameter_path
    for field in ("failed_routing_step",):
        safe = _safe_diagnostic_text(value.get(field))
        if safe:
            result[field] = safe
    limit_source = _safe_diagnostic_text(value.get("limit_source"), _SAFE_LIMIT_SOURCE)
    if limit_source:
        result["limit_source"] = limit_source
    source_kind = value.get("rate_limit_source")
    if source_kind in {"OPENROUTER", "UPSTREAM_PROVIDER", "UNKNOWN"}:
        result["rate_limit_source"] = source_kind
    for field in ("retry_after_at", "rate_limit_reset_at"):
        stamp = value.get(field)
        if isinstance(stamp, str) and len(stamp) <= 32:
            try:
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is not None:
                result[field] = parsed.astimezone(timezone.utc).isoformat()
    seconds = value.get("retry_after_seconds")
    if (type(seconds) in (int, float) and 0 <= seconds <= 604800
            and math.isfinite(seconds)):
        result["retry_after_seconds"] = int(seconds)
    request_id = _safe_diagnostic_text(value.get("request_id"), _SAFE_REQUEST_ID)
    if request_id:
        result["request_id"] = request_id
    funnel = value.get("routing_funnel")
    if isinstance(funnel, list):
        safe_funnel = []
        for item in funnel[:12]:
            if not isinstance(item, dict):
                continue
            safe_item = {}
            step = _safe_diagnostic_text(item.get("step"))
            if step:
                safe_item["step"] = step
            count = item.get("endpoint_count")
            if type(count) is int and 0 <= count <= 1_000_000:
                safe_item["endpoint_count"] = count
            provider = item.get("provider_slug", item.get("provider"))
            provider = _safe_diagnostic_text(provider, _SAFE_PROVIDER_SLUG)
            if provider:
                safe_item["provider"] = provider
            if step or provider:
                safe_funnel.append(safe_item)
        if safe_funnel:
            result["routing_funnel"] = safe_funnel
    for field in ("requested_providers", "available_providers"):
        providers = value.get(field)
        if isinstance(providers, list):
            safe = []
            for provider in providers[:16]:
                slug = _safe_diagnostic_text(provider, _SAFE_PROVIDER_SLUG)
                if slug:
                    safe.append(slug)
            if safe:
                result[field] = safe
    return result or None


def _response_diagnostics(response):
    """Extract bounded routing fields without retaining error text or prompts."""
    diagnostics = {"http_status": response.status_code}
    if response.status_code == 429:
        diagnostics.update({"limit_source": "unknown", "rate_limit_source": "UNKNOWN"})
        retry_after = response.headers.get("Retry-After")
        reset_at = response.headers.get("X-RateLimit-Reset")
        if retry_after:
            diagnostics["retry_after_at"] = _retry_at({"Retry-After": retry_after})
        if reset_at:
            diagnostics["rate_limit_reset_at"] = _retry_at({"X-RateLimit-Reset": reset_at})
    for header in ("x-openrouter-request-id", "x-request-id"):
        value = _safe_diagnostic_text(response.headers.get(header), _SAFE_REQUEST_ID)
        if value:
            diagnostics["request_id"] = value
            break
    body = response.content
    if len(body) <= _ERROR_BODY_LIMIT:
        try:
            payload = json.loads(body)
        except (ValueError, TypeError, UnicodeDecodeError):
            payload = None
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            metadata = error.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            extracted = {
                "http_status": response.status_code,
                "error_code": error.get("code"),
                "failed_routing_step": metadata.get("failed_routing_step"),
                "routing_funnel": metadata.get("routing_funnel"),
                "requested_providers": metadata.get("requested_providers"),
                "available_providers": metadata.get("available_providers"),
                "request_id": diagnostics.get("request_id"),
            }
            error_type = _safe_diagnostic_text(
                error.get("type"), re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$"),
            )
            if error_type:
                extracted["error_type"] = error_type
            parameter = error.get("param")
            if isinstance(parameter, str) and _SAFE_SCHEMA_PATH.fullmatch(parameter):
                extracted["parameter_path"] = parameter
            message = error.get("message")
            if response.status_code == 400 and isinstance(message, str) and len(message) <= 2048:
                normalized_message = message.casefold()
                if "schema" in normalized_message and any(
                        marker in normalized_message for marker in ("invalid", "unsupported", "must be")):
                    extracted["error_category"] = "STRUCTURED_OUTPUT_SCHEMA_REJECTED"
                elif "parameter" in normalized_message and any(
                        marker in normalized_message for marker in ("invalid", "unsupported", "not supported")):
                    extracted["error_category"] = "UNSUPPORTED_REQUEST_PARAMETER"
                elif "rate limit" in normalized_message or "rate-limited" in normalized_message:
                    extracted["error_category"] = "RATE_LIMIT"
                else:
                    extracted["error_category"] = "PROVIDER_ERROR"
                for keyword in (
                    "additionalProperties", "enum", "items", "maxItems", "maxLength",
                    "minItems", "minLength", "required", "type", "uniqueItems",
                ):
                    if re.search(rf"\b{re.escape(keyword.casefold())}\b", normalized_message):
                        extracted["schema_keyword"] = keyword
                        break
            if response.status_code == 429:
                source = metadata.get("limit_source")
                safe_source = _safe_diagnostic_text(source, _SAFE_LIMIT_SOURCE)
                extracted["limit_source"] = safe_source or "unknown"
                extracted["rate_limit_source"] = (
                    "OPENROUTER" if safe_source and safe_source.startswith("openrouter_")
                    else "UPSTREAM_PROVIDER" if safe_source and safe_source.startswith("upstream_")
                    else "UNKNOWN"
                )
                retry_seconds = metadata.get("retry_after_seconds")
                if (type(retry_seconds) in (int, float) and 0 <= retry_seconds <= 604800
                        and math.isfinite(retry_seconds)):
                    extracted["retry_after_seconds"] = retry_seconds
                extracted["retry_after_at"] = _retry_at(
                    {"Retry-After": response.headers.get("Retry-After")}
                    if response.headers.get("Retry-After") else {},
                )
                extracted["rate_limit_reset_at"] = _retry_at(
                    {"X-RateLimit-Reset": response.headers.get("X-RateLimit-Reset")}
                    if response.headers.get("X-RateLimit-Reset") else {},
                )
                if not extracted["retry_after_at"] and extracted.get("retry_after_seconds") is not None:
                    extracted["retry_after_at"] = datetime.fromtimestamp(
                        datetime.now(timezone.utc).timestamp() + extracted["retry_after_seconds"],
                        timezone.utc,
                    ).isoformat()
            diagnostics = sanitize_provider_diagnostics(extracted) or diagnostics
    return sanitize_provider_diagnostics(diagnostics)


class OpenRouterError(RuntimeError):
    """Sanitized adapter failure. Provider response text is never retained."""

    def __init__(self, code: str, *, retry_at: str | None = None, usage=None,
                 diagnostics=None):
        safe = code if code in {
            "AI_KEY_MISSING", "AI_MODEL_UNAVAILABLE", "AI_PRICING_UNVERIFIED",
            "AI_POLICY_DENIED", "AI_RATE_LIMITED", "AI_PROVIDER_UNAVAILABLE",
            "AI_INVALID_OUTPUT",
        } else "AI_PROVIDER_UNAVAILABLE"
        super().__init__(safe)
        self.code = safe
        self.retry_at = retry_at
        self.usage = usage if isinstance(usage, dict) else None
        self.diagnostics = sanitize_provider_diagnostics(diagnostics)


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
    reset_header = headers.get("X-RateLimit-Reset")
    raw = retry_after or reset_header
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
    if retry_after is None and reset_header is not None and seconds > 1_000_000_000_000:
        seconds /= 1000
    if retry_after is None and seconds > 1_000_000_000:
        if seconds < now.timestamp() or seconds > now.timestamp() + 7 * 24 * 60 * 60:
            return None
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    if seconds < 0 or seconds > 7 * 24 * 60 * 60:
        return None
    if retry_after is None and reset_header is not None and seconds > 7 * 24 * 60 * 60:
        return None
    return datetime.fromtimestamp(now.timestamp() + seconds, timezone.utc).isoformat()


class OpenRouterAdapter:
    """Provider boundary with exact-model, price, JSON, and profile checks."""

    def __init__(self, api_key: str | None, *, client: httpx.Client | None = None,
                 data_profile: str = STRICT_PRIVATE):
        if data_profile not in {STRICT_PRIVATE, PUBLIC_DEMO, PUBLIC_DATA}:
            raise ValueError("unsupported AI data profile")
        self._api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
        self.data_profile = data_profile
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(20.0, connect=5.0))
        self._completion_state = local()

    @property
    def last_generation_id(self):
        """Generation ID from the last completion in the current thread."""
        return getattr(self._completion_state, "generation_id", None)

    @last_generation_id.setter
    def last_generation_id(self, value):
        self._completion_state.generation_id = value

    @property
    def last_resolved_model(self):
        """Resolved model ID from the last completion in the current thread."""
        return getattr(self._completion_state, "resolved_model", None)

    @last_resolved_model.setter
    def last_resolved_model(self, value):
        self._completion_state.resolved_model = value

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
            diagnostics = _response_diagnostics(response)
            retry_at = ((diagnostics or {}).get("retry_after_at")
                        or (diagnostics or {}).get("rate_limit_reset_at")
                        or _retry_at(response.headers))
            raise OpenRouterError("AI_RATE_LIMITED", retry_at=retry_at,
                                  diagnostics=diagnostics)
        if response.status_code in (401, 403) and authenticated:
            raise OpenRouterError("AI_KEY_MISSING", diagnostics=_response_diagnostics(response)) from None
        if response.status_code < 200 or response.status_code >= 300:
            diagnostics = _response_diagnostics(response)
            failed_step = (diagnostics or {}).get("failed_routing_step", "").casefold()
            code = ("AI_POLICY_DENIED" if response.status_code == 404 and failed_step in {
                "filter by allowed providers", "filter by data policy", "filter by zdr",
            } else "AI_PROVIDER_UNAVAILABLE")
            raise OpenRouterError(code, diagnostics=diagnostics) from None
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

        zdr_endpoints = None
        if self.data_profile == STRICT_PRIVATE:
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
            json_supported = (isinstance(parameters, list)
                              and _REQUIRED_STRUCTURED_PARAMETERS.issubset(set(parameters)))
            if not _all_zero_pricing(endpoint.get("pricing")):
                pricing_unverified = True
                continue
            if (not isinstance(provider_name, str) or not provider_name.strip()
                    or not isinstance(provider_tag, str) or not provider_tag.strip()
                    or not json_supported or type(context_tokens) is not int or context_tokens <= 0
                    or type(max_output_tokens) is not int or max_output_tokens <= 0):
                continue
            if self.data_profile == PUBLIC_DEMO:
                policy_match = (model_id, provider_name, provider_tag) in _PUBLIC_DEMO_MODEL_ENDPOINTS
            elif self.data_profile == PUBLIC_DATA:
                policy_match = (provider_name, provider_tag) in _PUBLIC_DATA_PROVIDER_ENDPOINTS
            else:
                policy_match = next((entry for entry in zdr_endpoints
                    if isinstance(entry, dict)
                    and entry.get("model_id") == base_model_id
                    and entry.get("provider_name") == provider_name
                    and entry.get("tag") == provider_tag
                    and entry.get("safety_retention") is False), None)
            if policy_match:
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
        self.last_generation_id = None
        self.last_resolved_model = None
        if self._api_key is None:
            raise OpenRouterError("AI_KEY_MISSING")
        if (not isinstance(verified, VerifiedModel) or max_tokens < 1
                or max_tokens > verified.max_output_tokens):
            raise OpenRouterError("AI_MODEL_UNAVAILABLE")
        provider_policy = {
            "only": [verified.provider_tag],
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": {"prompt": 0, "completion": 0},
        }
        if self.data_profile == STRICT_PRIVATE:
            provider_policy.update({"data_collection": "deny", "zdr": True})
        else:
            provider_policy["data_collection"] = "allow"
        body = {
            "model": verified.model_id,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "trendscout_ai_output", "strict": True, "schema": schema},
            },
            "provider": provider_policy,
        }
        payload, response = self._request(
            "POST", "/chat/completions", authenticated=True, json=body,
            headers={"Content-Type": "application/json"},
        )
        generation_id = payload.get("id")
        if isinstance(generation_id, str) and _SAFE_GENERATION_ID.fullmatch(generation_id):
            self.last_generation_id = generation_id
        resolved_model = payload.get("model")
        if isinstance(resolved_model, str) and _SAFE_MODEL_METADATA.fullmatch(resolved_model):
            self.last_resolved_model = resolved_model
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

    def generation_metadata(self, generation_id: str) -> dict | None:
        """Fetch only bounded, billable metadata for a successful completion."""
        if (not self._api_key or not isinstance(generation_id, str)
                or not _SAFE_GENERATION_ID.fullmatch(generation_id)):
            return None
        payload, _ = self._request(
            "GET", f"/generation?id={quote(generation_id, safe='')}", authenticated=True,
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        result = {}
        returned_id = data.get("id")
        if isinstance(returned_id, str) and _SAFE_GENERATION_ID.fullmatch(returned_id):
            result["id"] = returned_id
        model = data.get("model")
        if isinstance(model, str) and _SAFE_MODEL_METADATA.fullmatch(model):
            result["model"] = model
        provider_name = _safe_diagnostic_text(data.get("provider_name"))
        if provider_name:
            result["provider_name"] = provider_name
        for field in ("tokens_prompt", "tokens_completion"):
            count = data.get(field)
            if type(count) is int and 0 <= count <= 10_000_000:
                result[field] = count
        cost = _number(data.get("total_cost"))
        if cost is not None and 0 <= cost <= Decimal("1e12"):
            result["total_cost"] = float(cost)
        generation_time = _number(data.get("generation_time"))
        if generation_time is not None and 0 <= generation_time <= Decimal("86400"):
            result["generation_time"] = float(generation_time)
        return result or None

    def key_quota_metadata(self, *, strict: bool = False) -> dict | None:
        """Return only provider quota counters, excluding labels and key metadata."""
        if type(strict) is not bool:
            raise TypeError("strict must be a boolean")
        if self._api_key is None:
            return None
        try:
            payload, _ = self._request("GET", "/key", authenticated=True)
        except OpenRouterError:
            if strict:
                raise
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
