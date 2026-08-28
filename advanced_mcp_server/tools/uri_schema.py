"""``odoo://`` URI schema build/parse helpers."""

import re
from dataclasses import dataclass


@dataclass
class RecordFieldURI:
    model: str
    record_id: int
    field: str


class URIParseError(Exception):
    pass


RECORD_FIELD_URI_PATTERN = re.compile(
    r"^odoo://record/([a-zA-Z][a-zA-Z0-9_.]*)/(\d+)/([a-zA-Z][a-zA-Z0-9_]*)$"
)
ATTACHMENT_URI_PATTERN = re.compile(r"^odoo://attachment/(\d+)$")


def build_field_uri(model: str, record_id: int, field: str) -> str:
    if not _is_valid_model_name(model):
        raise ValueError(f"Invalid model name: {model}")
    if not field:
        raise ValueError("Field name is required")
    return f"odoo://record/{model}/{record_id}/{field}"


def parse_field_uri(uri: str) -> RecordFieldURI:
    match = RECORD_FIELD_URI_PATTERN.match(uri)
    if not match:
        raise URIParseError(f"Invalid record field URI: {uri}")
    model, record_id_str, field = match.groups()
    return RecordFieldURI(model=model, record_id=int(record_id_str), field=field)


def build_attachment_uri(attachment_id: int) -> str:
    return f"odoo://attachment/{attachment_id}"


def parse_attachment_uri(uri: str) -> int:
    match = ATTACHMENT_URI_PATTERN.match(uri)
    if not match:
        raise URIParseError(f"Invalid attachment URI: {uri}")
    return int(match.group(1))


def _is_valid_model_name(model: str) -> bool:
    if not model:
        return False
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9_.]*$", model))
