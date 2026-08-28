"""Smart field selection for LLM-friendly payloads."""

import logging
from typing import Any, Dict, List

_logger = logging.getLogger(__name__)

DEFAULT_MAX_SMART_FIELDS = 15

ESSENTIAL_FIELDS = ["id", "name", "display_name", "active"]

SENSITIVE_FIELD_MARKERS = (
    "password",
    "passwd",
    "secret",
    "apikey",
    "token",
)

SENSITIVE_MARKER_SEQUENCES = (
    ("api", "key"),
    ("private", "key"),
    ("secret", "key"),
    ("access", "key"),
)

_BOOLEAN_FLAG_PREFIXES = ("is", "has", "can")


def is_sensitive_field_name(field_name: str) -> bool:
    segments = field_name.lower().split("_")
    if segments[-1] == "id" or segments[0] in _BOOLEAN_FLAG_PREFIXES:
        return False
    for sequence in SENSITIVE_MARKER_SEQUENCES:
        width = len(sequence)
        if any(
            tuple(segments[i : i + width]) == sequence
            for i in range(len(segments) - width + 1)
        ):
            return True
    return segments[-1] in SENSITIVE_FIELD_MARKERS


def score_field_importance(field_name: str, field_info: Dict[str, Any]) -> int:
    if field_name in ESSENTIAL_FIELDS:
        return 1000

    exclude_prefixes = ("_", "message_", "activity_", "website_message_")
    if field_name.startswith(exclude_prefixes):
        return 0

    exclude_fields = {
        "write_date",
        "create_date",
        "write_uid",
        "create_uid",
        "__last_update",
        "access_token",
        "access_warning",
        "access_url",
    }
    if field_name in exclude_fields:
        return 0

    if is_sensitive_field_name(field_name):
        return 0

    score = 0

    if field_info.get("required"):
        score += 500

    field_type = field_info.get("type", "")
    type_scores = {
        "char": 200,
        "boolean": 180,
        "selection": 170,
        "integer": 160,
        "float": 160,
        "monetary": 140,
        "date": 150,
        "datetime": 150,
        "many2one": 120,
        "text": 80,
    }
    score += type_scores.get(field_type, 50)

    if field_info.get("store", True):
        score += 80
    if field_info.get("searchable", True):
        score += 40

    business_patterns = [
        "state",
        "status",
        "stage",
        "priority",
        "company",
        "currency",
        "amount",
        "total",
        "date",
        "user",
        "partner",
        "email",
        "phone",
        "address",
        "street",
        "city",
        "country",
        "code",
        "ref",
        "number",
    ]
    if any(pattern in field_name.lower() for pattern in business_patterns):
        score += 60

    if not field_info.get("store", True):
        score = min(score, 30)

    if field_type in ("binary", "image", "html"):
        return 0

    if field_type in ("one2many", "many2many"):
        return 0

    return max(score, 0)


def get_smart_default_fields(
    fields_info: Dict[str, Dict[str, Any]],
    max_fields: int = DEFAULT_MAX_SMART_FIELDS,
) -> List[str]:
    field_scores = []
    for field_name, field_info in fields_info.items():
        score = score_field_importance(field_name, field_info)
        if score > 0:
            field_scores.append((field_name, score))

    field_scores.sort(key=lambda x: x[1], reverse=True)

    selected_fields = [field_name for field_name, _ in field_scores[:max_fields]]

    for field in ESSENTIAL_FIELDS:
        if field in fields_info and field not in selected_fields:
            selected_fields.append(field)

    _logger.debug(
        "Smart default fields: %s of %s fields (max configured: %s)",
        len(selected_fields),
        len(fields_info),
        max_fields,
    )
    return selected_fields
