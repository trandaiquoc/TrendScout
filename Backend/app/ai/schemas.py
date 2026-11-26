"""Strict JSON schemas and validators for provider outputs."""

from __future__ import annotations

import json
import math
import re


CLASSIFICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["classifications", "proposals"],
    "properties": {
        "classifications": {
            "type": "array",
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
                    "keyword_ids": {"type": "array", "minItems": 2, "items": {"type": "string"}},
                    "reason": {"type": "string", "maxLength": 500},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "uncertainty": {"type": "string", "minLength": 1, "maxLength": 500},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
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
                                             "items": {"type": "string"}},
                    "supporting_evidence_ids": {"type": "array",
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
    r"(?:\$|\b(?:USD|dollars?|revenue|sales|conversion|margin|market size|search volume|"
    r"demand|purchase intent|buyers?|will sell|guarantee(?:d)?|profit(?:able|ability)?|"
    r"units sold|price|pricing|certified|certification)\b|"
    r"\b\d+(?:[.,]\d+)?\s*(?:%|percent|million|billion|thousand)\b|\b\d{2,}\b)",
    re.IGNORECASE,
)


class InvalidAIOutput(ValueError):
    pass


def _reject_constant(_value):
    raise InvalidAIOutput("invalid JSON number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidAIOutput("duplicate JSON key")
        result[key] = value
    return result


def parse_json(text: str):
    if not isinstance(text, str) or len(text.encode("utf-8", "strict")) > 64_000:
        raise InvalidAIOutput("output exceeds limit")
    try:
        return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise InvalidAIOutput("invalid JSON") from exc


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
        raise InvalidAIOutput("unexpected classification object")
    rows = value["classifications"]
    proposals = value["proposals"]
    expected = set(keyword_ids)
    if not isinstance(rows, list) or len(rows) != len(expected) or not isinstance(proposals, list):
        raise InvalidAIOutput("classification count mismatch")
    normalized = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"keyword_id", "collection_id", "score", "reason"}:
            raise InvalidAIOutput("invalid classification row")
        keyword_id = row["keyword_id"]
        collection_id = row["collection_id"]
        if not isinstance(keyword_id, str) or keyword_id not in expected or keyword_id in normalized:
            raise InvalidAIOutput("unknown or duplicate keyword ID")
        if collection_id is not None and (not isinstance(collection_id, str) or collection_id not in collection_ids):
            raise InvalidAIOutput("unknown collection ID")
        if not _score(row["score"]) or not _bounded_text(row["reason"], 400):
            raise InvalidAIOutput("invalid classification score or reason")
        if _FORBIDDEN_NUMERIC_CLAIM.search(row["reason"]):
            raise InvalidAIOutput("unsupported classification claim")
        normalized[keyword_id] = {
            "keyword_id": keyword_id,
            "collection_id": collection_id,
            "score": float(row["score"]),
            "reason": row["reason"].strip(),
        }
    if set(normalized) != expected:
        raise InvalidAIOutput("missing keyword IDs")

    checked_proposals = []
    proposed_ids = set()
    for proposal in proposals:
        required = {"name", "description", "keyword_ids", "reason", "score", "uncertainty", "evidence_ids"}
        if not isinstance(proposal, dict) or set(proposal) != required:
            raise InvalidAIOutput("invalid proposal")
        members, refs = proposal["keyword_ids"], proposal["evidence_ids"]
        if (not isinstance(members, list) or len(members) < 2
                or any(not isinstance(item, str) for item in members)
                or len(members) != len(set(members)) or any(item not in expected for item in members)
                or not isinstance(refs, list) or any(not isinstance(item, str) for item in refs)
                or not refs or len(refs) != len(set(refs)) or any(item not in evidence_ids for item in refs)
                or any(normalized[item]["collection_id"] is not None for item in members)
                or any(item in proposed_ids for item in members)):
            raise InvalidAIOutput("invalid proposal references")
        if evidence_by_keyword is not None:
            supported_refs = {reference for keyword_id in members
                              for reference in evidence_by_keyword.get(keyword_id, ())}
            if any(reference not in supported_refs for reference in refs):
                raise InvalidAIOutput("proposal evidence does not support its keywords")
        if (not _bounded_text(proposal["name"], 80, minimum=3)
                or not _bounded_text(proposal["description"], 1200)
                or not _bounded_text(proposal["reason"], 500, minimum=1)
                or not _score(proposal["score"])
                or not _bounded_text(proposal["uncertainty"], 500, minimum=1)):
            raise InvalidAIOutput("invalid proposal fields")
        if any(_FORBIDDEN_NUMERIC_CLAIM.search(proposal[field])
               for field in ("name", "description", "reason", "uncertainty")):
            raise InvalidAIOutput("unsupported proposal claim")
        proposed_ids.update(members)
        checked_proposals.append({
            "name": proposal["name"].strip(),
            "description": proposal["description"].strip(),
            "keyword_ids": list(members),
            "reason": proposal["reason"].strip(),
            "score": float(proposal["score"]),
            "uncertainty": proposal["uncertainty"].strip(),
            "evidence_ids": list(refs),
        })
    return {"classifications": list(normalized.values()), "proposals": checked_proposals}


def validate_recommendations(value, *, keyword_ids, evidence_ids, evidence_by_keyword=None):
    if not isinstance(value, dict) or set(value) != {"recommendations"}:
        raise InvalidAIOutput("unexpected recommendation object")
    rows = value["recommendations"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5:
        raise InvalidAIOutput("recommendation count out of range")
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
    for row in rows:
        if not isinstance(row, dict) or set(row) != required:
            raise InvalidAIOutput("invalid recommendation fields")
        if any(not _bounded_text(row[field], 700 if field == "why_this_product" else 500,
                                 minimum=1 if field in ("product_name", "product_category", "why_this_product", "uncertainty") else 0)
               for field in text_fields):
            raise InvalidAIOutput("invalid recommendation text")
        if any(not isinstance(row[field], list) or len(row[field]) > 8
               or any(not _bounded_text(item, 300, minimum=1) for item in row[field])
               for field in list_fields):
            raise InvalidAIOutput("invalid recommendation list")
        related, refs = row["related_keyword_ids"], row["supporting_evidence_ids"]
        if (not isinstance(related, list) or not related
                or any(not isinstance(item, str) for item in related)
                or len(related) != len(set(related)) or any(item not in keyword_ids for item in related)
                or not isinstance(refs, list) or any(not isinstance(item, str) for item in refs)
                or not refs or len(refs) != len(set(refs))
                or any(item not in evidence_ids for item in refs)):
            raise InvalidAIOutput("invalid recommendation references")
        if evidence_by_keyword is not None:
            related_evidence = {reference for keyword_id in related
                                for reference in evidence_by_keyword.get(keyword_id, ())}
            if any(reference not in related_evidence for reference in refs):
                raise InvalidAIOutput("recommendation evidence does not support its keywords")
        narrative = [row[field] for field in text_fields]
        narrative.extend(item for field in list_fields for item in row[field])
        if any(_FORBIDDEN_NUMERIC_CLAIM.search(text) for text in narrative):
            raise InvalidAIOutput("unsupported quantitative or commercial claim")
        result.append({key: (list(value) if isinstance(value, list) else value.strip()
                             if isinstance(value, str) else value)
                       for key, value in row.items()})
    return {"recommendations": result}
