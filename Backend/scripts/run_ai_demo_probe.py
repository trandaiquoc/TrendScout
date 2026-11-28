"""Run one owner-authorized, two-call PUBLIC_DATA demo probe against PostgreSQL.

The command requires explicit operator authorization and reuses a completed
classification from the durable PostgreSQL probe when resuming. Provider quota
can remain UNKNOWN; durable internal reservations and the verified free model
route bound each outbound request. Missing generation metadata leaves cost
UNKNOWN and does not discard a valid response.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


MODEL_ID = "particle-ai/particle-1:free"
SOURCE_ID = "wikimedia_pageviews_top_per_country"


def _emit(payload: dict) -> None:
    print(json.dumps(payload, sort_keys=True))


def _safe_failure(exc) -> dict:
    result = {"status": "BLOCKED", "error_code": getattr(exc, "code", None)}
    if not isinstance(result["error_code"], str):
        result["error_code"] = type(exc).__name__
    retry_at = getattr(exc, "retry_at", None)
    if isinstance(retry_at, str) and len(retry_at) <= 64:
        result["retry_at"] = retry_at
    diagnostics = getattr(exc, "diagnostics", None)
    if isinstance(diagnostics, dict):
        for key in ("limit_source", "rate_limit_source", "retry_after_at",
                    "rate_limit_reset_at", "retry_after_seconds"):
            value = diagnostics.get(key)
            if isinstance(value, (str, int)) and len(str(value)) <= 80:
                result[key] = value
    return result


def _verify_generation(provider, *, generation_id, resolved_model):
    if not isinstance(generation_id, str) or not isinstance(resolved_model, str):
        return None
    try:
        metadata = provider.generation_metadata(generation_id)
    except Exception:
        return None
    from app.ai.runtime import resolved_model_matches_alias

    if (not isinstance(metadata, dict) or metadata.get("id") != generation_id
            or not resolved_model_matches_alias(MODEL_ID, metadata.get("model"))
            or metadata.get("model") != resolved_model
            or metadata.get("provider_name") != "Particle AI"
            or type(metadata.get("total_cost")) not in (int, float)):
        return None
    return metadata


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authorize-live-demo-probe", action="store_true", required=True,
                        help="explicitly authorize the fixed classification + recommendation pair")
    parser.add_argument("--schema", default="trendscout",
                        help="existing PostgreSQL application schema (default: trendscout)")
    parser.add_argument("--keyword-id", action="append",
                        help="optional existing LIVE Wikimedia keyword ID; defaults to the durable probe's classification")
    args = parser.parse_args(argv)
    if args.keyword_id and (
            len(args.keyword_id) != 1 or not isinstance(args.keyword_id[0], str)):
        _emit({"status": "BLOCKED", "error_code": "AI_OPERATOR_PROBE_SCOPE_INVALID"})
        return 2

    try:
        from app.ai.openrouter import PUBLIC_DATA, OpenRouterAdapter, OpenRouterError
        from app.ai.runtime import AIConfig, AIExecutionError, AIService
        from app.collections.postgres_store import PostgresCollectionStore
        from app.local_environment import load_local_environment
        from app.storage.postgres_atomic_state import PostgresAtomicStateStore
        from app.storage.postgres_config import postgres_environment

        env_file = BACKEND.parent / ".env"
        local_values = {}
        load_local_environment(env_file, local_values)
        api_key = local_values.get("OPENROUTER_API_KEY")
        if not isinstance(api_key, str) or not api_key.strip():
            _emit({"status": "BLOCKED", "error_code": "AI_KEY_MISSING"})
            return 2
        database_environment = postgres_environment({}, env_file)
        state_store = PostgresAtomicStateStore(database_environment, schema=args.schema)
        collection_store = PostgresCollectionStore(
            database_environment, schema=args.schema, data_mode_scope="LIVE",
        )
        config = AIConfig(
            enabled=True,
            collection_model=MODEL_ID,
            recommendation_model=MODEL_ID,
            allowed_sources=frozenset({SOURCE_ID}),
            transfer_approved_sources=frozenset({SOURCE_ID}),
            api_key_present=True,
            data_profile=PUBLIC_DATA,
            operator_probe_enabled=True,
        )
        provider = OpenRouterAdapter(api_key, data_profile=PUBLIC_DATA)
        service = AIService(state_store, config=config, provider=provider)
        try:
            stored_state = service._read_state()
            probe = (stored_state.get("operator_probe")
                     if isinstance(stored_state, dict) else None)
            keyword_ids = args.keyword_id or []
            if not keyword_ids and isinstance(probe, dict) and (
                    probe.get("status") in {"CLASSIFICATION_COMPLETE", "RECOMMENDATION_AUTHORIZED"}
                    and probe.get("requests_reserved") == 1
                    and isinstance(probe.get("attempt_ids"), list)
                    and len(probe["attempt_ids"]) == 1):
                classification_attempt = next((item for item in stored_state.get("attempts", [])
                                                if isinstance(item, dict)
                                                and item.get("id") == probe["attempt_ids"][0]), None)
                stored_job = stored_state.get("jobs", {}).get(
                    (classification_attempt or {}).get("job_id"),
                )
                candidate_ids = stored_job.get("input_ids") if isinstance(stored_job, dict) else None
                if (isinstance(candidate_ids, list) and len(candidate_ids) == 1
                        and isinstance(candidate_ids[0], str)):
                    keyword_ids = candidate_ids
            if len(keyword_ids) != 1 or not isinstance(keyword_ids[0], str):
                _emit({"status": "BLOCKED", "error_code": "AI_OPERATOR_PROBE_SCOPE_INVALID"})
                return 2

            # These are read-only provider checks. The key usage amount is not
            # interpreted as a request count or remaining quota. The selected
            # model's price and provider route, not account-plan metadata, guard
            # against paid inference.
            verified = provider.verify_model(MODEL_ID)
            provider.key_quota_metadata(strict=False)
            if not (verified.supports_json_schema
                    and verified.provider_name == "Particle AI"
                    and verified.provider_tag == "particle-ai/us"):
                _emit({"status": "BLOCKED", "error_code": "AI_MODEL_UNAVAILABLE"})
                return 2

            validation = service.classify_keyword_ids(
                collection_store, keyword_ids, preview=True, validate_only=True,
            )
            eligible = validation.get("eligible")
            if (not isinstance(eligible, list) or len(eligible) != 1
                    or eligible[0].get("id") != keyword_ids[0]
                    or eligible[0].get("source") != SOURCE_ID
                    or eligible[0].get("data_mode") != "LIVE"
                    or not eligible[0].get("evidence_ids")):
                _emit({"status": "BLOCKED", "error_code": "AI_ACTIVE_EVIDENCE_UNAVAILABLE"})
                return 2

            resumed_classification = False
            resume_authorized = False
            classification = None
            classification_job = None
            authorized = None
            if (isinstance(probe, dict)
                    and probe.get("status") in {"CLASSIFICATION_COMPLETE",
                                                  "RECOMMENDATION_AUTHORIZED"}
                    and probe.get("requests_reserved") == 1
                    and isinstance(probe.get("attempt_ids"), list)
                    and len(probe["attempt_ids"]) == 1):
                classification_attempt = next((item for item in stored_state.get("attempts", [])
                                                if isinstance(item, dict)
                                                and item.get("id") == probe["attempt_ids"][0]), None)
                classification_job_id = (classification_attempt or {}).get("job_id")
                stored_job = stored_state.get("jobs", {}).get(classification_job_id)
                if (isinstance(classification_attempt, dict)
                        and classification_attempt.get("task") == "classification"
                        and classification_attempt.get("outcome") == "COMPLETED"
                        and isinstance(stored_job, dict)
                        and stored_job.get("task") == "classification"
                        and stored_job.get("status") == "COMPLETED"
                        and stored_job.get("input_ids") == keyword_ids
                        and isinstance(stored_job.get("result"), dict)
                        and stored_job["result"].get("mode") == "PREVIEW"
                        and stored_job["result"].get("processed") == 1):
                    classification_job = service.get_job(classification_job_id)
                    classification = {
                        "status": "COMPLETED", "mode": "PREVIEW", "processed": 1,
                        "job_id": classification_job_id,
                    }
                    resumed_classification = True
                    resume_authorized = probe.get("status") == "RECOMMENDATION_AUTHORIZED"
                    authorized = {"status": "RESUMED", "provider_quota_status": "UNKNOWN"}

            if not resumed_classification:
                authorized = service.authorize_bounded_operator_probe(authorized=True)
                classification = service.classify_keyword_ids(
                    collection_store, keyword_ids,
                    idempotency_key="demo-live-classification-" + uuid.uuid4().hex,
                    preview=True,
                )
            if (classification.get("status") != "COMPLETED"
                    or classification.get("mode") != "PREVIEW"
                    or classification.get("processed") != 1):
                _emit({"operator_probe": authorized, "classification": _safe_failure(
                    AIExecutionError(classification.get("error_code") or "AI_CLASSIFICATION_FAILED",
                                     retry_at=classification.get("retry_at"))),
                    "status": "BLOCKED", "recommendation": "NOT_SENT"})
                return 2

            classification_job = classification_job or service.get_job(classification["job_id"])
            model_lineage = ((classification_job or {}).get("lineage") or {}).get("model_provider")
            generation_id = (model_lineage or {}).get("generation_id")
            resolved_model = (model_lineage or {}).get("resolved_model")
            if (not resumed_classification
                    and (generation_id != provider.last_generation_id
                         or resolved_model != provider.last_resolved_model)):
                _emit({"status": "BLOCKED", "error_code": "AI_GENERATION_LINEAGE_UNAVAILABLE",
                       "recommendation": "NOT_SENT"})
                return 2
            classification_generation = _verify_generation(
                provider, generation_id=generation_id, resolved_model=resolved_model,
            )
            if (classification_generation is not None
                    and classification_generation.get("total_cost") != 0):
                _emit({"status": "BLOCKED", "error_code": "AI_GENERATION_COST_NONZERO",
                       "recommendation": "NOT_SENT"})
                return 2

            if not resume_authorized:
                service.authorize_operator_recommendation(
                    classification_job=classification_job,
                    generation_metadata=classification_generation,
                )
            recommendation = service.recommend(
                collection_store, keyword_ids, research_constraints="",
                idempotency_key="demo-live-recommendation-" + uuid.uuid4().hex,
                regenerate=True,
            )
            if (recommendation.get("status") != "COMPLETED"
                    or recommendation.get("processed") != 1
                    or not recommendation.get("recommendations")):
                error = AIExecutionError(
                    recommendation.get("error_code") or "AI_RECOMMENDATION_FAILED",
                    retry_at=recommendation.get("retry_at"),
                    diagnostics=recommendation.get("diagnostics"),
                )
                _emit({"status": "BLOCKED", "operator_probe": "CLOSED_OR_INCOMPLETE",
                       "classification": "PASS", "recommendation": _safe_failure(error)})
                return 2

            recommendation_lineage = (recommendation.get("lineage") or {}).get("model_provider")
            recommendation_generation = _verify_generation(
                provider,
                generation_id=(recommendation_lineage or {}).get("generation_id"),
                resolved_model=(recommendation_lineage or {}).get("resolved_model"),
            )
            if (recommendation_generation is not None
                    and recommendation_generation.get("total_cost") != 0):
                _emit({"status": "BLOCKED", "operator_probe": "INCOMPLETE",
                       "classification": "PASS", "recommendation": "PASS_SCHEMA_COST_NONZERO"})
                return 2

            service.complete_bounded_operator_probe(
                recommendation_result=recommendation,
                generation_metadata=recommendation_generation,
            )
            final_caps = service.capabilities()
            _emit({
                "status": "PASS",
                "operator_probe": "COMPLETE",
                "provider_quota_status": "UNKNOWN",
                "automatic_ai_enabled": final_caps.get("enabled") is True,
                "authorized_requests": 2,
                "classification": {"status": "PASS", "processed": 1,
                    "generation_id": generation_id, "resolved_model": resolved_model,
                    "provider": "Particle AI",
                    "actual_cost": (classification_generation.get("total_cost")
                                    if classification_generation else None),
                    "cost_status": "VERIFIED_ZERO" if classification_generation else "UNKNOWN"},
                "recommendation": {"status": "PASS",
                    "recommendation_count": len(recommendation["recommendations"]),
                    "generation_id": (recommendation_lineage or {}).get("generation_id"),
                    "resolved_model": (recommendation_lineage or {}).get("resolved_model"),
                    "provider": "Particle AI",
                    "actual_cost": (recommendation_generation.get("total_cost")
                                    if recommendation_generation else None),
                    "cost_status": "VERIFIED_ZERO" if recommendation_generation else "UNKNOWN"},
                "internal_daily_limit": 40,
                "internal_minute_limit": 20,
                "provider_metadata_is_request_quota": False,
            })
            return 0
        finally:
            provider.close()
    except (AIExecutionError, OpenRouterError) as exc:
        _emit(_safe_failure(exc))
        return 2
    except Exception as exc:
        # Database and network exceptions may contain credentials or SQL text.
        _emit({"status": "BLOCKED", "error_code": type(exc).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
