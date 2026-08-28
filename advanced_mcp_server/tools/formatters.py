"""Data formatters for LLM-friendly output.

These formatters convert Odoo data (plain dicts as returned by ``read`` plus
``fields_get`` metadata) into a hierarchical text format optimized for LLM
consumption. They take only plain data — no live Odoo connection.
"""

import json
from datetime import date, datetime
from typing import Any, Dict, List, Optional


class RecordFormatter:
    """Formats Odoo records for LLM consumption."""

    OMIT_FIELDS = {
        "__last_update",
        "write_date",
        "create_date",
        "write_uid",
        "create_uid",
        "message_follower_ids",
        "message_ids",
        "message_main_attachment_id",
    }

    BINARY_FIELDS = {"binary", "image", "file"}

    MAX_FIELD_DISPLAY_LENGTH = 2000

    def __init__(self, model: str):
        self.model = model

    def format_record(
        self,
        record: Dict[str, Any],
        fields_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
        indent_level: int = 0,
        related_summaries: Optional[Dict[str, List[tuple]]] = None,
        enabled_relations: Optional[set] = None,
    ) -> str:
        lines = []
        indent = "  " * indent_level
        related_summaries = related_summaries or {}

        record_id = record.get("id", "Unknown")
        record_name = (
            record.get("display_name") or record.get("name") or f"Record {record_id}"
        )

        lines.append(f"{indent}{'=' * 50}")
        lines.append(f"{indent}Record: {self.model}/{record_id}")
        lines.append(f"{indent}Name: {record_name}")
        lines.append(f"{indent}{'=' * 50}")

        simple_fields = []
        relation_fields = []

        for field_name, field_value in record.items():
            if field_name in self.OMIT_FIELDS or field_name.startswith("_"):
                continue
            if field_name in ("id", "name", "display_name"):
                continue

            field_meta = fields_metadata.get(field_name, {}) if fields_metadata else {}
            field_type = field_meta.get("type", "unknown")

            if field_type in ("many2one", "one2many", "many2many"):
                relation_fields.append((field_name, field_value, field_meta))
            else:
                simple_fields.append((field_name, field_value, field_meta))

        if simple_fields:
            lines.append(f"{indent}Fields:")
            for field_name, field_value, field_meta in simple_fields:
                formatted_value = self._format_field_value(
                    field_name, field_value, field_meta
                )
                lines.append(f"{indent}  {field_name}: {formatted_value}")

        if relation_fields:
            lines.append(f"{indent}Relationships:")
            for field_name, field_value, field_meta in relation_fields:
                lines.extend(
                    self._format_relation_field(
                        field_name,
                        field_value,
                        field_meta,
                        indent_level + 1,
                        related_summaries.get(field_name),
                        parent_id=record.get("id"),
                        enabled_relations=enabled_relations,
                    )
                )

        return "\n".join(lines)

    def _format_field_value(
        self, field_name: str, value: Any, field_meta: Dict[str, Any]
    ) -> str:
        field_type = field_meta.get("type", "unknown")

        if field_type == "boolean" or (
            field_type == "unknown" and isinstance(value, bool)
        ):
            return "Yes" if value else "No"

        if value is None or value is False:
            return "Not set"

        if field_type in ("char", "text", "html"):
            return self._truncate_value(str(value))

        elif field_type in ("integer", "float", "monetary"):
            return self._format_numeric_value(value, field_type, field_meta)

        elif field_type in ("date", "datetime"):
            return self._format_datetime_value(value, field_type)

        elif field_type == "selection":
            selection = field_meta.get("selection", [])
            for key, label in selection:
                if key == value:
                    return f"{label} ({value})"
            return str(value)

        elif field_type in ("one2many", "many2many"):
            if isinstance(value, list):
                return f"{len(value)} record(s)" if value else "No records"
            return str(value)

        elif field_type in self.BINARY_FIELDS:
            return f"[Binary data - use {self.model}/{field_name} to retrieve]"

        else:
            if (
                isinstance(value, (list, tuple))
                and len(value) == 2
                and isinstance(value[0], int)
                and isinstance(value[1], str)
            ):
                return f"{value[1]} (ID: {value[0]})"
            return self._truncate_value(str(value))

    def _format_numeric_value(
        self, value: Any, field_type: str, field_meta: Dict[str, Any]
    ) -> str:
        if field_type == "monetary":
            return f"{value:,.2f}"
        if field_type == "float":
            digits = field_meta.get("digits", (16, 2))
            precision = (
                digits[1]
                if isinstance(digits, (tuple, list)) and len(digits) == 2
                else 2
            )
            return f"{value:,.{precision}f}"
        return f"{value:,}"

    def _format_datetime_value(self, value: Any, field_type: str) -> str:
        if isinstance(value, str):
            if field_type == "datetime" and " " in value:
                try:
                    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
                    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
                except ValueError:
                    return value
            return value
        if isinstance(value, (datetime, date)):
            if isinstance(value, datetime):
                return value.strftime("%Y-%m-%dT%H:%M:%S+00:00")
            return value.isoformat()
        return str(value)

    def _truncate_value(self, text: str) -> str:
        if len(text) > self.MAX_FIELD_DISPLAY_LENGTH:
            return (
                text[: self.MAX_FIELD_DISPLAY_LENGTH]
                + f"... [truncated, {len(text)} chars total]"
            )
        return text

    def _format_relation_field(
        self,
        field_name: str,
        value: Any,
        field_meta: Dict[str, Any],
        indent_level: int,
        summary: Optional[List[tuple]] = None,
        parent_id: Optional[int] = None,
        enabled_relations: Optional[set] = None,
    ) -> List[str]:
        lines = []
        indent = "  " * indent_level
        field_type = field_meta.get("type", "unknown")

        if field_type == "many2one":
            if value and isinstance(value, (list, tuple)) and len(value) == 2:
                related_id, related_name = value
                related_model = field_meta.get("relation", "unknown")
                related_ref = f"{related_name} ({related_model},{related_id})"
                lines.append(f"{indent}{field_name}: {related_ref}")
            else:
                lines.append(f"{indent}{field_name}: Not set")

        elif field_type in ("one2many", "many2many"):
            if value and isinstance(value, list):
                count = len(value)
                related_model = field_meta.get("relation", "unknown")
                lines.append(f"{indent}{field_name}: {count} record(s)")

                if summary:
                    lines.append(f"{indent}  Items:")
                    for idx, (rid, rname) in enumerate(summary, 1):
                        lines.append(f"{indent}    [{idx}] {rname} (id {rid})")
                elif related_model != "unknown":
                    lines.extend(
                        self._view_all_hint(
                            indent,
                            field_type,
                            field_meta,
                            value,
                            related_model,
                            parent_id,
                            enabled_relations,
                        )
                    )
            else:
                lines.append(f"{indent}{field_name}: No records")

        return lines

    def _view_all_hint(
        self,
        indent: str,
        field_type: str,
        field_meta: Dict[str, Any],
        value: List[int],
        related_model: str,
        parent_id: Optional[int],
        enabled_relations: Optional[set],
    ) -> List[str]:
        if enabled_relations is not None and related_model not in enabled_relations:
            return [
                f"{indent}  (related model '{related_model}' is not enabled "
                f"for MCP access, so its records cannot be listed)"
            ]

        relation_field = field_meta.get("relation_field")
        if field_type == "one2many" and relation_field and isinstance(parent_id, int):
            domain_str = json.dumps([[relation_field, "=", parent_id]])
            suffix = ""
        else:
            related_ids = [item for item in value if isinstance(item, int)]
            domain_str = json.dumps([["id", "in", related_ids[:50]]])
            suffix = " (first 50 ids)" if len(related_ids) > 50 else ""
        return [
            f"{indent}  → View all: use the search_records tool with "
            f"model='{related_model}', domain={domain_str}{suffix}"
        ]

    def _get_record_summary(self, record: Dict[str, Any]) -> str:
        summary_fields = [
            "display_name",
            "name",
            "complete_name",
            "partner_id",
            "title",
        ]
        for field in summary_fields:
            if field in record and record[field]:
                value = record[field]
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    return f"{value[1]} (ID: {value[0]})"
                elif isinstance(value, str):
                    return value
        return f"ID: {record.get('id', 'Unknown')}"


class DatasetFormatter:
    """Formats datasets and search results for LLM consumption."""

    def __init__(self, model: str):
        self.model = model
        self.record_formatter = RecordFormatter(model)

    def format_search_results(
        self,
        records: List[Dict[str, Any]],
        domain: Optional[List] = None,
        fields: Optional[List[str]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        total_count: Optional[int] = None,
        fields_metadata: Optional[Dict[str, Any]] = None,
        next_hint: Optional[str] = None,
        prev_hint: Optional[str] = None,
        current_page: Optional[int] = None,
        total_pages: Optional[int] = None,
    ) -> str:
        lines = [
            f"{'=' * 60}",
            f"Search Results: {self.model}",
            f"{'=' * 60}",
        ]

        if domain:
            lines.append(f"Search criteria: {self._format_domain(domain)}")

        if total_count is not None:
            showing = len(records)
            if current_page and total_pages:
                lines.append(f"Page {current_page} of {total_pages}")
            if offset is not None:
                lines.append(
                    f"Showing records {offset + 1}-{offset + showing} of {total_count}"
                )
            else:
                lines.append(f"Showing {showing} of {total_count} records")
        else:
            lines.append(f"Found {len(records)} records")

        if fields:
            lines.append(f"Fields: {', '.join(fields)}")

        lines.append("")

        if not records:
            lines.append("No records found matching the criteria.")
        else:
            lines.extend(
                self._format_result_records(records, fields, offset, fields_metadata)
            )

        navigation = []
        if prev_hint:
            navigation.append(f"← Previous page: {prev_hint}")
        if next_hint:
            navigation.append(f"→ Next page: {next_hint}")

        if navigation:
            lines.append("\nNavigation:")
            lines.extend(navigation)

        if total_count and total_count > 100:
            lines.append("\nDataset Summary:")
            lines.append(f"Total records: {total_count:,}")
            if domain:
                lines.append("Use additional filters to refine results")

        return "\n".join(lines)

    def _format_result_records(
        self,
        records: List[Dict[str, Any]],
        fields: Optional[List[str]],
        offset: Optional[int],
        fields_metadata: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        metadata = fields_metadata or {}
        lines = []
        for idx, record in enumerate(records, 1):
            if offset:
                idx = offset + idx
            lines.append(f"[{idx}] {self.record_formatter._get_record_summary(record)}")

            if fields and len(fields) <= 5:
                for field in fields:
                    if field in record and field not in (
                        "id",
                        "name",
                        "display_name",
                    ):
                        formatted = self.record_formatter._format_field_value(
                            field, record[field], metadata.get(field, {})
                        )
                        lines.append(f"    {field}: {formatted}")

            lines.append("")
        return lines

    def _format_domain(self, domain: List) -> str:
        if not domain:
            return "All records"

        conditions = []
        for condition in domain:
            if isinstance(condition, (list, tuple)) and len(condition) == 3:
                field, operator, value = condition
                conditions.append(f"{field} {operator} {value}")
            elif condition in ("&", "|", "!"):
                conditions.append(condition)

        return " ".join(conditions) if conditions else str(domain)
