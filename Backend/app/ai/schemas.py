"""Strict JSON schemas and validators for provider outputs."""

from __future__ import annotations

import json
import math
import re
from copy import deepcopy

CLASSIFICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["classifications", "proposals"],
    "properties": {
        "classifications": {
            "type": "array",
            "minItems": 0,
            "maxItems": 40,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["keyword_id", "collection_id", "score", "reason"],
                "properties": {
                    "keyword_id": {"type": "string"},
                    "collection_id": {"type": ["string", "null"]},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "reason": {"type": "string", "maxLength": 400},
                },
            },
        },
        "proposals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "description", "keyword_ids", "reason", "score",
                             "uncertainty", "evidence_ids"],
                "properties": {
                    "name": {"type": "string", "minLength": 3, "maxLength": 80},
                    "description": {"type": "string", "maxLength": 1200},
                    "keyword_ids": {"type": "array", "minItems": 2, "uniqueItems": True,
                                     "items": {"type": "string"}},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "uncertainty": {"type": "string", "minLength": 1, "maxLength": 500},
                    "evidence_ids": {"type": "array", "minItems": 1,
                                      "uniqueItems": True, "items": {"type": "string"}},
                },
            },
        },
    },
}

RECOMMENDATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["recommendations"],
    "properties": {
        "recommendations": {
            "type": "array",
            "minItems": 1,
            "maxItems": 5,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["product_name", "product_category", "related_keyword_ids",
                             "supporting_evidence_ids", "why_this_product", "consumer_segment",
                             "seasonality", "possible_materials", "manufacturing_considerations",
                             "export_considerations", "competition_risks", "ip_risks",
                             "regulatory_risks", "uncertainty", "research_steps"],
                "properties": {
                    "product_name": {"type": "string", "minLength": 2, "maxLength": 120},
                    "product_category": {"type": "string", "minLength": 2, "maxLength": 120},
                    "related_keyword_ids": {"type": "array", "minItems": 1,
                                             "uniqueItems": True,
                                             "items": {"type": "string"}},
                    "supporting_evidence_ids": {"type": "array",
                                                "minItems": 1, "uniqueItems": True,
                                                "items": {"type": "string"}},
                    "why_this_product": {"type": "string", "minLength": 1, "maxLength": 700},
                    "consumer_segment": {"type": "string", "maxLength": 300},
                    "seasonality": {"type": "string", "maxLength": 300},
                    "possible_materials": {"type": "array", "maxItems": 8,
                                            "items": {"type": "string", "maxLength": 120}},
                    "manufacturing_considerations": {"type": "array", "maxItems": 8,
                                                      "items": {"type": "string", "maxLength": 300}},
                    "export_considerations": {"type": "array", "maxItems": 8,
                                              "items": {"type": "string", "maxLength": 300}},
                    "competition_risks": {"type": "array", "maxItems": 8,
                                          "items": {"type": "string", "maxLength": 300}},
                    "ip_risks": {"type": "array", "maxItems": 8,
                                 "items": {"type": "string", "maxLength": 300}},
                    "regulatory_risks": {"type": "array", "maxItems": 8,
                                          "items": {"type": "string", "maxLength": 300}},
                    "uncertainty": {"type": "string", "minLength": 1, "maxLength": 500},
                    "research_steps": {"type": "array", "minItems": 1, "maxItems": 8,
                                       "items": {"type": "string", "maxLength": 300}},
                },
            },
        },
    },
}

_FORBIDDEN_NUMERIC_CLAIM = re.compile(
    r"(?:\$\s*\d|\bUSD\s*\d|\b\d+(?:[.,]\d+)?\s*dollars?\b|"
    r"\b\d+(?:[.,]\d+)?\s*(?:%|percent|million|billion|thousand)\b|\b\d{2,}\b)",
    re.IGNORECASE,
)
_UNSUPPORTED_COMMERCIAL_ASSERTION = re.compile(
    r"\bguarantee(?:d)?(?:\s+\w+){0,3}\s+(?:sales?|revenue|buyers?|profits?|margins?)\b|"
    r"\b(?:estimated|projected|forecast|predicted)\s+(?:\w+\s+){0,3}"
    r"(?:sales?|revenue|market size|demand|buyers?|profit|margin)\b|"
    r"\b(?:demand|sales?|revenue|profit(?:ability)?|margins?|buyers?|conversion|"
    r"market size|price|pricing)\b(?:\s+\w+){0,2}\s+"
    r"(?:is|are|was|were|will be|has been|have been)\s+"
    r"(?:high|low|strong|growing|increasing|increased|rising|guaranteed|profitable|"
    r"competitive|popular|surging|booming|proven|certain)\b|"
    r"\b(?:buyers?|customers?)\s+(?:prefer|want|choose|purchase|buy|seek)\b|"
    r"\b(?:is|are|was|were|has been|have been)\s+(?:not\s+)?certified\b|"
    r"\bcertified\s+(?:product|goods?|item)\b",
    re.IGNORECASE,
)


class InvalidAIOutput(ValueError):
    """Output contract failure carrying bounded, content-free diagnostics."""

    def __init__(self, reason_code, *, field_path="$", expected_constraint="valid_output",
                 validation_stage="APPLICATION", reference_category=None,
                 reference_status=None):
        super().__init__(reason_code)
        self.diagnostics = {
            "validation_stage": validation_stage,
            "reason_code": reason_code,
            "field_path": field_path,
            "expected_constraint": expected_constraint,
        }
        if reference_category is not None:
            self.diagnostics["reference_category"] = reference_category
        if reference_status is not None:
            self.diagnostics["reference_status"] = reference_status


def classification_schema_for(*, keyword_ids, collection_ids, evidence_ids):
    schema = deepcopy(CLASSIFICATION_SCHEMA)
    keyword_ids = sorted(set(keyword_ids))
    collection_ids = sorted(set(collection_ids))
    evidence_ids = sorted(set(evidence_ids))
    rows = schema["properties"]["classifications"]
    rows.update(minItems=len(keyword_ids), maxItems=len(keyword_ids))
    row_properties = rows["items"]["properties"]
    row_properties["keyword_id"]["enum"] = keyword_ids
    row_properties["collection_id"]["enum"] = [None, *collection_ids]
    proposal_array = schema["properties"]["proposals"]
    proposal_array.update(minItems=0, maxItems=len(keyword_ids) // 2)
    if len(keyword_ids) >= 2:
        proposal = proposal_array["items"]["properties"]
        proposal["keyword_ids"].update(maxItems=len(keyword_ids), uniqueItems=True)
        proposal["keyword_ids"]["items"]["enum"] = keyword_ids
        proposal["evidence_ids"].update(minItems=1, maxItems=len(evidence_ids), uniqueItems=True)
        proposal["evidence_ids"]["items"]["enum"] = evidence_ids
    return schema


def recommendation_schema_for(*, keyword_ids, evidence_ids):
    schema = deepcopy(RECOMMENDATION_SCHEMA)
    keyword_ids = sorted(set(keyword_ids))
    evidence_ids = sorted(set(evidence_ids))
    row = schema["properties"]["recommendations"]["items"]["properties"]
    row["related_keyword_ids"].update(maxItems=len(keyword_ids), uniqueItems=True)
    row["related_keyword_ids"]["items"]["enum"] = keyword_ids
    row["supporting_evidence_ids"].update(
        minItems=1, maxItems=len(evidence_ids), uniqueItems=True,
    )
    row["supporting_evidence_ids"]["items"]["enum"] = evidence_ids
    return schema


def _reject_constant(_value):
    raise InvalidAIOutput("INVALID_JSON_NUMBER", validation_stage="JSON_PARSE",
                          expected_constraint="finite_json_number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidAIOutput("DUPLICATE_JSON_KEY", validation_stage="JSON_PARSE",
                                  expected_constraint="unique_object_keys")
        result[key] = value
    return result


def parse_json(text: str):
    if not isinstance(text, str):
        raise InvalidAIOutput("INVALID_JSON_TYPE", validation_stage="JSON_PARSE",
                              expected_constraint="utf8_json_string")
    try:
        encoded_length = len(text.encode("utf-8", "strict"))
    except UnicodeError as exc:
        raise InvalidAIOutput("INVALID_JSON_ENCODING", validation_stage="JSON_PARSE",
                              expected_constraint="valid_utf8") from exc
    if encoded_length > 64_000:
        raise InvalidAIOutput("OUTPUT_TOO_LARGE", validation_stage="JSON_PARSE",
                              expected_constraint="utf8_bytes_at_most_64000")
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise InvalidAIOutput("INVALID_JSON", validation_stage="JSON_PARSE",
                              expected_constraint="valid_utf8_json") from exc


def _bounded_text(value, maximum, *, minimum=0):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        return False
    try:
        value.encode("utf-8", "strict")
    except UnicodeError:
        return False
    return not any(ord(character) < 32 and character not in "\n\t" for character in value)


def _score(value):
    return (type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1)


def validate_classification(value, *, keyword_ids, collection_ids, evidence_ids, evidence_by_keyword=None):
    if not isinstance(value, dict) or set(value) != {"classifications", "proposals"}:
        raise InvalidAIOutput("UNEXPECTED_OBJECT_FIELDS", field_path="$",
                              expected_constraint="classifications_and_proposals_only")
    rows, proposals = value["classifications"], value["proposals"]
    expected = set(keyword_ids)
    if not isinstance(rows, list):
        raise InvalidAIOutput("INVALID_LIST", field_path="classifications",
                              expected_constraint="array_of_classification_rows")
    if not isinstance(proposals, list) or len(proposals) > len(expected) // 2:
        raise InvalidAIOutput("LIST_LENGTH_OUT_OF_RANGE", field_path="proposals",
                              expected_constraint="zero_to_half_keyword_count")
    actual_ids = {row.get("keyword_id") for row in rows
                  if isinstance(row, dict) and isinstance(row.get("keyword_id"), str)}
    missing_ids, unknown_ids = expected - actual_ids, actual_ids - expected
    if len(rows) != len(expected) or missing_ids or unknown_ids:
        raise InvalidAIOutput(
            "CLASSIFICATION_SET_MISMATCH", field_path="classifications",
            expected_constraint="exactly_one_row_per_requested_keyword_id",
            reference_category="KEYWORD_ID",
            reference_status="MISSING" if missing_ids else "UNKNOWN",
        )

    normalized = {}
    row_fields = {"keyword_id", "collection_id", "score", "reason"}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) != row_fields:
            raise InvalidAIOutput("INVALID_OBJECT_FIELDS", field_path=f"classifications[{index}]",
                                  expected_constraint="classification_row_contract")
        keyword_id, collection_id = row["keyword_id"], row["collection_id"]
        if keyword_id in normalized:
            raise InvalidAIOutput("DUPLICATE_REFERENCE", field_path=f"classifications[{index}].keyword_id",
                                  expected_constraint="unique_requested_keyword_id",
                                  reference_category="KEYWORD_ID", reference_status="DUPLICATE")
        if collection_id is not None and (not isinstance(collection_id, str)
                                          or collection_id not in collection_ids):
            raise InvalidAIOutput("UNKNOWN_REFERENCE",
                                  field_path=f"classifications[{index}].collection_id",
                                  expected_constraint="null_or_known_collection_id",
                                  reference_category="COLLECTION_ID", reference_status="UNKNOWN")
        if not _score(row["score"]):
            raise InvalidAIOutput("INVALID_SCORE", field_path=f"classifications[{index}].score",
                                  expected_constraint="finite_number_between_0_and_1")
        if not _bounded_text(row["reason"], 400):
            raise InvalidAIOutput("INVALID_TEXT_LENGTH_OR_ENCODING",
                                  field_path=f"classifications[{index}].reason",
                                  expected_constraint="utf8_string_at_most_400")
        claim_reason = _unsupported_claim_reason(row["reason"])
        if claim_reason:
            raise InvalidAIOutput(claim_reason, field_path=f"classifications[{index}].reason",
                                  expected_constraint="no_unverified_numeric_or_commercial_claim")
        normalized[keyword_id] = {
            "keyword_id": keyword_id, "collection_id": collection_id,
            "score": float(row["score"]), "reason": row["reason"].strip(),
        }

    checked_proposals, proposed_ids = [], set()
    proposal_fields = {"name", "description", "keyword_ids", "reason", "score", "uncertainty", "evidence_ids"}
    for index, proposal in enumerate(proposals):
        base_path = f"proposals[{index}]"
        if not isinstance(proposal, dict) or set(proposal) != proposal_fields:
            raise InvalidAIOutput("INVALID_OBJECT_FIELDS", field_path=base_path,
                                  expected_constraint="proposal_contract")
        members, refs = proposal["keyword_ids"], proposal["evidence_ids"]
        if not isinstance(members, list) or len(members) < 2 or len(members) > len(expected):
            raise InvalidAIOutput("LIST_LENGTH_OUT_OF_RANGE", field_path=f"{base_path}.keyword_ids",
                                  expected_constraint="two_to_keyword_count_items")
        if any(not isinstance(item, str) for item in members):
            raise InvalidAIOutput("INVALID_REFERENCE_TYPE", field_path=f"{base_path}.keyword_ids",
                                  expected_constraint="string_keyword_ids",
                                  reference_category="KEYWORD_ID", reference_status="UNKNOWN")
        if len(members) != len(set(members)):
            raise InvalidAIOutput("DUPLICATE_REFERENCE", field_path=f"{base_path}.keyword_ids",
                                  expected_constraint="unique_keyword_ids",
                                  reference_category="KEYWORD_ID", reference_status="DUPLICATE")
        if any(item not in expected for item in members):
            raise InvalidAIOutput("UNKNOWN_REFERENCE", field_path=f"{base_path}.keyword_ids",
                                  expected_constraint="requested_keyword_ids_only",
                                  reference_category="KEYWORD_ID", reference_status="UNKNOWN")
        if any(normalized[item]["collection_id"] is not None or item in proposed_ids for item in members):
            raise InvalidAIOutput("UNSUPPORTED_REFERENCE", field_path=f"{base_path}.keyword_ids",
                                  expected_constraint="unassigned_nonoverlapping_keyword_ids",
                                  reference_category="KEYWORD_ID", reference_status="UNSUPPORTED")
        if not isinstance(refs, list) or not refs:
            raise InvalidAIOutput("MISSING_REFERENCE", field_path=f"{base_path}.evidence_ids",
                                  expected_constraint="one_or_more_known_evidence_ids",
                                  reference_category="EVIDENCE_ID", reference_status="MISSING")
        if any(not isinstance(item, str) for item in refs):
            raise InvalidAIOutput("INVALID_REFERENCE_TYPE", field_path=f"{base_path}.evidence_ids",
                                  expected_constraint="string_evidence_ids",
                                  reference_category="EVIDENCE_ID", reference_status="UNKNOWN")
        if len(refs) != len(set(refs)):
            raise InvalidAIOutput("DUPLICATE_REFERENCE", field_path=f"{base_path}.evidence_ids",
                                  expected_constraint="unique_evidence_ids",
                                  reference_category="EVIDENCE_ID", reference_status="DUPLICATE")
        if any(item not in evidence_ids for item in refs):
            raise InvalidAIOutput("UNKNOWN_REFERENCE", field_path=f"{base_path}.evidence_ids",
                                  expected_constraint="known_evidence_ids_only",
                                  reference_category="EVIDENCE_ID", reference_status="UNKNOWN")
        if evidence_by_keyword is not None:
            supported_refs = {reference for keyword_id in members
                              for reference in evidence_by_keyword.get(keyword_id, ())}
            if any(reference not in supported_refs for reference in refs):
                raise InvalidAIOutput("UNSUPPORTED_REFERENCE", field_path=f"{base_path}.evidence_ids",
                                      expected_constraint="evidence_belongs_to_proposed_keywords",
                                      reference_category="EVIDENCE_ID", reference_status="UNSUPPORTED")
        text_constraints = {
            "name": (80, 3), "description": (1200, 0),
            "reason": (500, 1), "uncertainty": (500, 1),
        }
        for field, (maximum, minimum) in text_constraints.items():
            if not _bounded_text(proposal[field], maximum, minimum=minimum):
                raise InvalidAIOutput("INVALID_TEXT_LENGTH_OR_ENCODING",
                                      field_path=f"{base_path}.{field}",
                                      expected_constraint=f"utf8_string_{minimum}_to_{maximum}")
        if not _score(proposal["score"]):
            raise InvalidAIOutput("INVALID_SCORE", field_path=f"{base_path}.score",
                                  expected_constraint="finite_number_between_0_and_1")
        for field in ("name", "description", "reason", "uncertainty"):
            claim_reason = _unsupported_claim_reason(proposal[field])
            if claim_reason:
                raise InvalidAIOutput(claim_reason, field_path=f"{base_path}.{field}",
                                      expected_constraint="no_unverified_numeric_or_commercial_claim")
        proposed_ids.update(members)
        checked_proposals.append({
            "name": proposal["name"].strip(), "description": proposal["description"].strip(),
            "keyword_ids": list(members), "reason": proposal["reason"].strip(),
            "score": float(proposal["score"]), "uncertainty": proposal["uncertainty"].strip(),
            "evidence_ids": list(refs),
        })
    return {"classifications": list(normalized.values()), "proposals": checked_proposals}


def validate_recommendations(value, *, keyword_ids, evidence_ids, evidence_by_keyword=None):
    if not isinstance(value, dict) or set(value) != {"recommendations"}:
        raise InvalidAIOutput("UNEXPECTED_OBJECT_FIELDS", field_path="$",
                              expected_constraint="recommendations_only")
    rows = value["recommendations"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5:
        raise InvalidAIOutput("LIST_LENGTH_OUT_OF_RANGE", field_path="recommendations",
                              expected_constraint="between_1_and_5_items")
    result = []
    required = {
        "product_name", "product_category", "related_keyword_ids", "supporting_evidence_ids",
        "why_this_product", "consumer_segment", "seasonality", "possible_materials",
        "manufacturing_considerations", "export_considerations", "competition_risks", "ip_risks",
        "regulatory_risks", "uncertainty", "research_steps",
    }
    text_fields = ("product_name", "product_category", "why_this_product", "consumer_segment",
                   "seasonality", "uncertainty")
    list_fields = ("possible_materials", "manufacturing_considerations", "export_considerations",
                   "competition_risks", "ip_risks", "regulatory_risks", "research_steps")
    text_limits = {
        "product_name": (120, 2), "product_category": (120, 2),
        "why_this_product": (700, 1), "consumer_segment": (300, 0),
        "seasonality": (300, 0), "uncertainty": (500, 1),
    }
    item_limits = {"possible_materials": 120, "manufacturing_considerations": 300,
                   "export_considerations": 300, "competition_risks": 300,
                   "ip_risks": 300, "regulatory_risks": 300, "research_steps": 300}
    for index, row in enumerate(rows):
        base_path = f"recommendations[{index}]"
        if not isinstance(row, dict) or set(row) != required:
            raise InvalidAIOutput("INVALID_OBJECT_FIELDS", field_path=base_path,
                                  expected_constraint="recommendation_contract")
        for field, (maximum, minimum) in text_limits.items():
            if not _bounded_text(row[field], maximum, minimum=minimum):
                raise InvalidAIOutput("INVALID_TEXT_LENGTH_OR_ENCODING",
                                      field_path=f"{base_path}.{field}",
                                      expected_constraint=f"utf8_string_{minimum}_to_{maximum}")
        for field, maximum_item_length in item_limits.items():
            values = row[field]
            if not isinstance(values, list) or len(values) > 8:
                raise InvalidAIOutput("INVALID_LIST_LENGTH_OR_ITEM", field_path=f"{base_path}.{field}",
                                      expected_constraint="zero_to_eight_items")
            for item_index, item in enumerate(values):
                if not _bounded_text(item, maximum_item_length, minimum=1):
                    raise InvalidAIOutput(
                        "INVALID_LIST_LENGTH_OR_ITEM",
                        field_path=f"{base_path}.{field}[{item_index}]",
                        expected_constraint=f"utf8_string_1_to_{maximum_item_length}",
                    )
        related, refs = row["related_keyword_ids"], row["supporting_evidence_ids"]
        if not isinstance(related, list) or not related:
            raise InvalidAIOutput("MISSING_REFERENCE", field_path=f"{base_path}.related_keyword_ids",
                                  expected_constraint="one_or_more_known_keyword_ids",
                                  reference_category="KEYWORD_ID", reference_status="MISSING")
        if any(not isinstance(item, str) for item in related):
            raise InvalidAIOutput("INVALID_REFERENCE_TYPE",
                                  field_path=f"{base_path}.related_keyword_ids",
                                  expected_constraint="string_keyword_ids",
                                  reference_category="KEYWORD_ID", reference_status="UNKNOWN")
        if len(related) != len(set(related)):
            raise InvalidAIOutput("DUPLICATE_REFERENCE",
                                  field_path=f"{base_path}.related_keyword_ids",
                                  expected_constraint="unique_keyword_ids",
                                  reference_category="KEYWORD_ID", reference_status="DUPLICATE")
        if any(item not in keyword_ids for item in related):
            raise InvalidAIOutput("UNKNOWN_REFERENCE",
                                  field_path=f"{base_path}.related_keyword_ids",
                                  expected_constraint="known_keyword_ids_only",
                                  reference_category="KEYWORD_ID", reference_status="UNKNOWN")
        if not isinstance(refs, list) or not refs:
            raise InvalidAIOutput("MISSING_REFERENCE",
                                  field_path=f"{base_path}.supporting_evidence_ids",
                                  expected_constraint="one_or_more_known_evidence_ids",
                                  reference_category="EVIDENCE_ID", reference_status="MISSING")
        if any(not isinstance(item, str) for item in refs):
            raise InvalidAIOutput("INVALID_REFERENCE_TYPE",
                                  field_path=f"{base_path}.supporting_evidence_ids",
                                  expected_constraint="string_evidence_ids",
                                  reference_category="EVIDENCE_ID", reference_status="UNKNOWN")
        if len(refs) != len(set(refs)):
            raise InvalidAIOutput("DUPLICATE_REFERENCE",
                                  field_path=f"{base_path}.supporting_evidence_ids",
                                  expected_constraint="unique_evidence_ids",
                                  reference_category="EVIDENCE_ID", reference_status="DUPLICATE")
        if any(item not in evidence_ids for item in refs):
            raise InvalidAIOutput("UNKNOWN_REFERENCE",
                                  field_path=f"{base_path}.supporting_evidence_ids",
                                  expected_constraint="known_evidence_ids_only",
                                  reference_category="EVIDENCE_ID", reference_status="UNKNOWN")
        if evidence_by_keyword is not None:
            related_evidence = {reference for keyword_id in related
                                for reference in evidence_by_keyword.get(keyword_id, ())}
            if any(reference not in related_evidence for reference in refs):
                raise InvalidAIOutput("UNSUPPORTED_REFERENCE",
                                      field_path=f"{base_path}.supporting_evidence_ids",
                                      expected_constraint="evidence_belongs_to_related_keywords",
                                      reference_category="EVIDENCE_ID", reference_status="UNSUPPORTED")
        for field in text_fields:
            claim_reason = _unsupported_claim_reason(row[field])
            if claim_reason:
                raise InvalidAIOutput(claim_reason, field_path=f"{base_path}.{field}",
                                      expected_constraint="no_unverified_numeric_or_commercial_claim")
        for field in list_fields:
            for item_index, item in enumerate(row[field]):
                claim_reason = _unsupported_claim_reason(item)
                if claim_reason:
                    raise InvalidAIOutput(
                        claim_reason, field_path=f"{base_path}.{field}[{item_index}]",
                        expected_constraint="no_unverified_numeric_or_commercial_claim",
                    )
        result.append({key: (list(value) if isinstance(value, list) else value.strip()
                             if isinstance(value, str) else value)
                       for key, value in row.items()})
    return {"recommendations": result}


def _unsupported_claim_reason(text):
    if _FORBIDDEN_NUMERIC_CLAIM.search(text):
        return "UNSUPPORTED_NUMERIC_CLAIM"
    if _UNSUPPORTED_COMMERCIAL_ASSERTION.search(text):
        return "UNSUPPORTED_COMMERCIAL_CLAIM"
    return None


def sanitize_validation_diagnostics(value):
    """Allow only validator-generated enums and paths; never include output text."""
    if not isinstance(value, dict):
        return None
    allowed_stages = {"APPLICATION", "JSON_PARSE"}
    allowed_categories = {
        "KEYWORD_ID", "COLLECTION_ID", "EVIDENCE_ID", "KEYWORD_OR_EVIDENCE_ID",
    }
    stage, reason = value.get("validation_stage"), value.get("reason_code")
    path, expected = value.get("field_path"), value.get("expected_constraint")
    if (stage not in allowed_stages or not isinstance(reason, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", reason)
            or not isinstance(path, str)
            or not re.fullmatch(r"[A-Za-z0-9_.$\[\]-]{1,180}", path)
            or not isinstance(expected, str)
            or not re.fullmatch(r"[a-z0-9_]{1,100}", expected)):
        return None
    result = {"validation_stage": stage, "reason_code": reason,
              "field_path": path, "expected_constraint": expected}
    category, status = value.get("reference_category"), value.get("reference_status")
    if category in allowed_categories:
        result["reference_category"] = category
    if status in {"MISSING", "UNKNOWN", "DUPLICATE", "UNSUPPORTED", "UNKNOWN_OR_INVALID"}:
        result["reference_status"] = status
    return result
