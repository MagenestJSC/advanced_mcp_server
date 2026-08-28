# Read-side capabilities for adv.tool.mixin:
# describe, get, search, aggregate, me, count, explain, compare, resources.
# schema, get, find, summary, session, total, timeline, compare, catalog.

import ast
import html
import json

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, MissingError, UserError

from ..server.helpers import (
    describe_session as build_user_context,
    list_accessible_models as get_enabled_models,
)
from ..tools.formatters import DatasetFormatter, RecordFormatter
from ..tools.smart_fields import (
    DEFAULT_MAX_SMART_FIELDS,
    get_smart_default_fields,
    is_sensitive_field_name,
)
from ..tools.uri_schema import build_attachment_uri, build_field_uri
from .tool_mixin import _BINARY_FIELD_TYPES, adv_tool

DEFAULT_LIMIT = 10
MAX_LIMIT = 100
MAX_OFFSET_PAGES = 1000
DEFAULT_MAX_RELATED_ITEMS = 3

_ALL_FIELDS_SENTINEL = "__all__"

_CURATED_FIELD_ATTRIBUTES = [
    "type",
    "string",
    "required",
    "readonly",
    "relation",
    "selection",
]

_SCHEMA_FIELD_ATTRIBUTES = _CURATED_FIELD_ATTRIBUTES + [
    "store",
    "searchable",
    "digits",
    "relation_field",
]


def _result(text: str, structured=None) -> dict:
    out = {"content": [{"type": "text", "text": text}]}
    if structured is not None:
        out["structuredContent"] = structured
    return out


def _parse_list_arg(text: str, kind: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return ast.literal_eval(text)
        except (ValueError, SyntaxError) as err:
            label = _("domain") if kind == "domain" else _("fields")
            raise UserError(
                _("Invalid %(label)s: expected a list, got %(text)s", label=label, text=text[:100])
            ) from err


class AdvDataCapabilities(models.AbstractModel):
    _inherit = "adv.tool.mixin"

    def _cfg_int(self, key: str, default: int) -> int:
        val = self.env["ir.config_parameter"].sudo().get_param(key, default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _limit_bounds(self) -> tuple[int, int]:
        lo = self._cfg_int("adv_mcp.default_limit", DEFAULT_LIMIT)
        hi = self._cfg_int("adv_mcp.max_limit", MAX_LIMIT)
        lo = lo if lo > 0 else DEFAULT_LIMIT
        hi = hi if hi > 0 else MAX_LIMIT
        return min(lo, hi), hi

    def _adv_live_input_schema(self, schema: dict) -> dict:
        props = schema.get("properties", {})
        limit = props.get("limit")
        desc = limit.get("description", "") if isinstance(limit, dict) else ""
        if "%(default)s" not in desc:
            return schema
        default_limit, max_limit = self._limit_bounds()
        filled = desc % {"default": default_limit, "max": max_limit}
        return {**schema, "properties": {**props, "limit": {**limit, "description": filled}}}

    @staticmethod
    def _unescape_domain(domain: list) -> list:
        # LLMs sometimes HTML-encode operators (<, >, &) — fix them before ORM sees them.
        result = []
        for item in domain:
            if isinstance(item, (list, tuple)) and len(item) == 3:
                field, op, val = item
                result.append([field, html.unescape(op) if isinstance(op, str) else op, val])
            else:
                result.append(item)
        return result

    @staticmethod
    def _coerce_domain(domain) -> list:
        if domain is None:
            return []
        if isinstance(domain, (list, tuple)):
            return AdvDataCapabilities._unescape_domain(list(domain))
        if isinstance(domain, str):
            text = domain.strip()
            if not text:
                return []
            parsed = _parse_list_arg(text, "domain")
            if not isinstance(parsed, (list, tuple)):
                raise UserError(_("Domain must be a list."))
            return AdvDataCapabilities._unescape_domain(list(parsed))
        raise UserError(_("Domain must be a list."))

    @staticmethod
    def _coerce_fields(fields):
        if fields is None:
            return None
        if isinstance(fields, str):
            text = fields.strip()
            if not text:
                return None
            fields = _parse_list_arg(text, "fields")
        if isinstance(fields, (list, tuple)):
            return list(fields) or None
        raise UserError(_("Fields must be a list of field names."))

    def _resolve_fields(self, fields, schema: dict) -> tuple[str, list | None]:
        fields = self._coerce_fields(fields)
        if fields is None:
            max_f = self._cfg_int("adv_mcp.max_smart_fields", DEFAULT_MAX_SMART_FIELDS)
            return "smart_defaults", get_smart_default_fields(schema, max_fields=max_f)
        if fields == [_ALL_FIELDS_SENTINEL]:
            return "all", None
        return "explicit", fields

    def _clamp_limit(self, limit) -> int:
        lo, hi = self._limit_bounds()
        if not limit:
            return lo
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise UserError(_("'limit' must be an integer.")) from None
        return lo if limit <= 0 else min(limit, hi)

    def _clamp_offset(self, offset) -> int:
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            raise UserError(_("'offset' must be an integer.")) from None
        _, hi = self._limit_bounds()
        return min(offset, hi * MAX_OFFSET_PAGES)

    @staticmethod
    def _drop_sensitive(record: dict):
        for name in [k for k in record if is_sensitive_field_name(k)]:
            del record[name]

    @staticmethod
    def _binary_names(schema: dict) -> set[str]:
        return {n for n, m in schema.items() if (m or {}).get("type") in _BINARY_FIELD_TYPES}

    # ── describe ───────────────────────────────────────────────────────────

    @adv_tool(
        name="describe",
        title="Describe",
        description=(
            "Without 'model': list every resource accessible through this gateway "
            "with its allowed operations. "
            "With 'model': return the full schema — fields, types, access rules — "
            "for that resource. Replaces the separate list_models and get_fields calls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {
                    "type": ["string", "null"],
                    "description": "Technical model name. Omit to list all accessible resources.",
                },
                "field_names": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Filter to these field names when inspecting a model.",
                },
            },
            "additionalProperties": False,
        },
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    @api.model
    def describe(self, model=None, field_names=None):
        if not model:
            return self._catalog_resources()
        return self._inspect_model(model, field_names)

    def _catalog_resources(self) -> dict:
        enriched = get_enabled_models(self.env)
        lines = ["=" * 60, _("Accessible resources (%s)", len(enriched)), "=" * 60]
        if not enriched:
            lines.append(_("No resources are currently accessible through this gateway."))
        for entry in enriched:
            ops = entry["operations"] or {}
            allowed = ", ".join(op for op in ("read", "create", "write", "unlink") if ops.get(op))
            lines.append(f"- {entry['name']} ({entry['model']}) [{allowed or _('no operations')}]")
        return _result("\n".join(lines), {"resources": enriched})

    def _inspect_model(self, model: str, field_names=None) -> dict:
        rs = self._resolve_model(model)
        self._check_op(model, "read")
        raw = rs.fields_get(field_names or None, attributes=list(_SCHEMA_FIELD_ATTRIBUTES))
        fields = [{"name": fname, **meta} for fname, meta in sorted(raw.items())]
        access = self._model_access_summary(model)
        structured = {
            "model": model,
            "fields": fields,
            "total_fields": len(fields),
            "access": access,
        }
        return _result(self._format_schema_text(model, fields, access), structured)

    @staticmethod
    def _model_access_summary(model: str) -> dict:
        # Returns display info only — actual enforcement is in _check_op
        return {"model": model, "note": "Use browse_schema without 'model' to see allowed ops"}

    @staticmethod
    def _format_schema_text(model: str, fields: list, access: dict) -> str:
        lines = ["=" * 60, _("Schema: %(model)s (%(n)s fields)", model=model, n=len(fields)), "=" * 60]
        for f in fields:
            ftype = f.get("type") or "?"
            rel = f.get("relation")
            type_part = f"{ftype}→{rel}" if rel else ftype
            line = f"{f['name']} ({type_part})"
            label = f.get("string")
            if label:
                line += f" — {label}"
            flags = [fl for fl in ("required", "readonly") if f.get(fl)]
            if flags:
                line += f" [{', '.join(flags)}]"
            sel = f.get("selection")
            if sel:
                line += ": " + ", ".join(str(s[0]) for s in sel)
            lines.append(line)
        return "\n".join(lines)

    # ── get ───────────────────────────────────────────────────────────────

    @adv_tool(
        name="get",
        title="Get",
        description=(
            "Retrieve a single record by ID. "
            "Use 'depth' (0–2) to auto-expand Many2one relations: "
            "depth=1 inlines the display_name of each Many2one, "
            "depth=2 fully reads the related record."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Technical model name."},
                "record_id": {"type": "integer", "description": "Record ID to retrieve."},
                "fields": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Fields to return; null for smart defaults.",
                },
                "depth": {
                    "type": "integer",
                    "default": 0,
                    "description": "Relation expansion depth (0=none, 1=name only, 2=full record).",
                },
            },
            "required": ["model", "record_id"],
            "additionalProperties": False,
        },
        operation="read",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    @api.model
    def fetch(self, model, record_id, fields=None, depth=0):
        rs = self._resolve_model(model)
        self._check_op(model, "read")
        schema = rs.fields_get(attributes=_SCHEMA_FIELD_ATTRIBUTES)
        method, to_read = self._resolve_fields(fields, schema)
        record = self._browse_record_or_raise(model, rs, record_id)
        data = record.with_context(bin_size=True).read(to_read)[0]
        if method != "explicit":
            self._drop_sensitive(data)
        render_meta = self._swap_binary_uris(model, int(record_id), data, schema)
        if depth:
            self._expand_relations(data, schema, int(depth))
        related = self._resolve_related_summaries(data, schema, self._cfg_int(
            "adv_mcp.max_related_items", DEFAULT_MAX_RELATED_ITEMS
        ))
        enabled_rels = self._enabled_relation_models(data, schema)
        text = RecordFormatter(model).format_record(
            data, render_meta, related_summaries=related, enabled_relations=enabled_rels
        )
        structured = {"record": data}
        if method == "smart_defaults":
            structured["metadata"] = {
                "fields_returned": len(data),
                "field_selection": method,
                "total_available": len(schema),
            }
        return _result(text, structured)

    def _expand_relations(self, data: dict, schema: dict, depth: int):
        if depth <= 0:
            return
        for name, val in list(data.items()):
            meta = schema.get(name) or {}
            if meta.get("type") != "many2one" or not isinstance(val, (list, tuple)):
                continue
            rel_id, _ = val[0], val[1]
            rel_model = meta.get("relation")
            if not rel_model or not rel_id:
                continue
            try:
                self._check_op(rel_model, "read")
                rel_rs = self.env[rel_model].browse(rel_id)
                if depth == 1:
                    data[name] = {"id": rel_id, "display_name": rel_rs.display_name}
                else:
                    inner = rel_rs.with_context(bin_size=True).read(None)
                    data[name] = inner[0] if inner else val
            except (AccessError, UserError):
                pass

    def _swap_binary_uris(self, model: str, record_id: int, data: dict, schema: dict) -> dict:
        render_meta = schema
        for name in self._binary_names(schema):
            if data.get(name):
                data[name] = build_field_uri(model, record_id, name)
                if render_meta is schema:
                    render_meta = dict(schema)
                render_meta[name] = {**schema[name], "type": "char"}
        return render_meta

    def _resolve_related_summaries(self, data: dict, schema: dict, max_items: int) -> dict:
        if max_items <= 0:
            return {}
        summaries = {}
        for name, value in data.items():
            meta = schema.get(name) or {}
            if meta.get("type") not in ("one2many", "many2many"):
                continue
            relation = meta.get("relation")
            if not relation or not isinstance(value, list):
                continue
            if not 0 < len(value) <= max_items:
                continue
            if not self._relation_read_ok(relation):
                continue
            try:
                related = self.env[relation].browse(value).read(["display_name"])
                summaries[name] = [
                    (r["id"], r.get("display_name") or f"id {r['id']}") for r in related
                ]
            except (AccessError, MissingError):
                pass
        return summaries

    def _enabled_relation_models(self, data: dict, schema: dict) -> set[str]:
        enabled = set()
        for name, value in data.items():
            meta = schema.get(name) or {}
            if meta.get("type") not in ("one2many", "many2many"):
                continue
            rel = meta.get("relation")
            if not rel or rel in enabled or not value:
                continue
            if self._relation_read_ok(rel):
                enabled.add(rel)
        return enabled

    def _relation_read_ok(self, relation: str) -> bool:
        try:
            self._resolve_model(relation)
            self._check_op(relation, "read")
        except (AccessError, UserError):
            return False
        return True

    # ── search ──────────────────────────────────────────────────────────────

    @adv_tool(
        name="search",
        title="Search",
        description=(
            "Search records in a resource. "
            "Use 'domain' for an Odoo domain filter, OR 'spec' for a simpler "
            'key-value dict (e.g. {"partner_id.country_id.name": "Vietnam", "active": true}). '
            "If both are given, they are combined with AND. "
            "Use 'fields', 'limit', 'offset', 'order' to shape the result."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Technical model name."},
                "domain": {
                    "type": ["array", "string", "null"],
                    "description": "Odoo domain filter list or JSON string.",
                },
                "spec": {
                    "type": ["object", "null"],
                    "description": 'Simple equality filter: {"field": value, ...}.',
                },
                "fields": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Fields to return; null for smart defaults.",
                },
                "limit": {
                    "type": ["integer", "null"],
                    "description": "Max rows. Defaults to %(default)s, capped at %(max)s.",
                },
                "offset": {"type": "integer", "default": 0},
                "order": {"type": ["string", "null"], "description": "Sort, e.g. 'name asc'."},
            },
            "required": ["model"],
            "additionalProperties": False,
        },
        operation="read",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    @api.model
    def search(self, model, domain=None, spec=None, fields=None, limit=None, offset=0, order=None):
        rs = self._resolve_model(model)
        self._check_op(model, "read")
        filter_expr = self._build_filter(domain, spec)
        limit = self._clamp_limit(limit)
        offset = self._clamp_offset(offset)
        schema = rs.fields_get(attributes=_SCHEMA_FIELD_ATTRIBUTES)
        method, to_read = self._resolve_fields(fields, schema)
        rows = rs.with_context(bin_size=True).search_read(
            filter_expr, to_read, offset=offset, limit=limit, order=order or None
        )
        if method != "explicit":
            for row in rows:
                self._drop_sensitive(row)
        total = (
            len(rows) if offset == 0 and (not limit or len(rows) < limit)
            else rs.search_count(filter_expr)
        )
        self._replace_binary_uris_in_rows(model, rows, schema)
        total_pages = (total + limit - 1) // limit if limit else 1
        current_page = (offset // limit) + 1 if limit else 1
        next_hint, prev_hint = self._page_hints("search", offset, limit, len(rows), total)
        text = DatasetFormatter(model).format_search_results(
            rows, domain=filter_expr or None, fields=to_read, limit=limit, offset=offset,
            total_count=total, fields_metadata=schema, next_hint=next_hint,
            prev_hint=prev_hint, current_page=current_page, total_pages=total_pages,
        )
        return _result(text, {"records": rows, "total": total, "limit": limit,
                               "offset": offset, "model": model})

    @staticmethod
    def _build_filter(domain, spec) -> list:
        base = []
        if domain is not None:
            raw = domain.strip() if isinstance(domain, str) else domain
            if isinstance(raw, str):
                parsed = _parse_list_arg(raw, "domain")
                if not isinstance(parsed, (list, tuple)):
                    raise UserError(_("Domain must be a list."))
                base = list(parsed)
            elif isinstance(raw, (list, tuple)):
                base = list(raw)
            base = AdvDataCapabilities._unescape_domain(base)
        spec_clauses = []
        if spec and isinstance(spec, dict):
            spec_clauses = [(k, "=", v) for k, v in spec.items()]
        return base + spec_clauses

    @staticmethod
    def _page_hints(tool: str, offset: int, limit: int, count: int, total: int):
        next_hint = prev_hint = None
        if offset + count < total:
            next_hint = _(
                "%(tool)s with offset=%(o)s, limit=%(l)s",
                tool=tool, o=offset + limit, l=limit,
            )
        if offset > 0:
            prev_hint = _(
                "%(tool)s with offset=%(o)s, limit=%(l)s",
                tool=tool, o=max(0, offset - limit), l=limit,
            )
        return next_hint, prev_hint

    def _replace_binary_uris_in_rows(self, model: str, rows: list, schema: dict):
        binary = self._binary_names(schema)
        if not binary:
            return
        for row in rows:
            row_id = row.get("id")
            for name in binary:
                if row_id and row.get(name):
                    row[name] = build_field_uri(model, row_id, name)

    # ── aggregate ─────────────────────────────────────────────────────────────

    @adv_tool(
        name="aggregate",
        title="Aggregate",
        description=(
            "Aggregate records by one or two grouping dimensions. "
            "With only 'row_groupby': returns a flat list of groups (like a simple pivot). "
            "With both 'row_groupby' and 'col_groupby': builds a cross-tab matrix "
            "where rows and columns are the two grouping keys."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Technical model name."},
                "row_groupby": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Row grouping fields, e.g. [\"stage_id\"].",
                },
                "col_groupby": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Column grouping fields for cross-tab. Omit for flat grouping.",
                },
                "aggregates": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": 'Aggregate expressions e.g. ["amount_total:sum"]. Defaults to ["__count"].',
                },
                "domain": {
                    "type": ["array", "string", "null"],
                    "description": "Odoo domain filter.",
                },
                "limit": {
                    "type": ["integer", "null"],
                    "description": "Max groups. Defaults to %(default)s, capped at %(max)s.",
                },
                "offset": {"type": "integer", "default": 0},
            },
            "required": ["model", "row_groupby"],
            "additionalProperties": False,
        },
        operation="read",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    @api.model
    def aggregate(self, model, row_groupby, col_groupby=None, aggregates=None, domain=None,
              limit=None, offset=0):
        rs = self._resolve_model(model)
        self._check_op(model, "read")
        if isinstance(row_groupby, str):
            row_groupby = [row_groupby]
        if not row_groupby:
            raise UserError(_("row_groupby must not be empty."))
        filter_expr = self._coerce_domain(domain)
        limit = self._clamp_limit(limit)
        offset = self._clamp_offset(offset)
        aggs = list(aggregates) if aggregates else ["__count"]
        if col_groupby:
            return self._cross_tab(rs, model, row_groupby, list(col_groupby), aggs, filter_expr)
        return self._flat_pivot(rs, model, row_groupby, aggs, filter_expr, limit, offset)

    def _flat_pivot(self, rs, model: str, groupby: list, aggs: list,
                    filter_expr: list, limit: int, offset: int) -> dict:
        raw_groups = rs.formatted_read_group(
            filter_expr, groupby=groupby, aggregates=aggs,
            offset=offset, limit=limit + 1 if limit else None,
        )
        has_more = bool(limit) and len(raw_groups) > limit
        groups = raw_groups[:limit] if has_more else raw_groups
        cleaned = [{k: v for k, v in g.items() if not k.startswith("__") or k in aggs}
                   for g in groups]
        text = self._format_pivot_text(model, groupby, aggs, cleaned, has_more)
        return _result(text, {"groups": cleaned, "model": model, "row_groupby": groupby,
                               "aggregates": aggs, "has_more": has_more})

    def _cross_tab(self, rs, model: str, row_groupby: list, col_groupby: list,
                   aggs: list, filter_expr: list) -> dict:
        combined = row_groupby + col_groupby
        raw = rs.formatted_read_group(filter_expr, groupby=combined, aggregates=aggs)
        col_vals = sorted({tuple(g.get(c) for c in col_groupby) for g in raw})
        row_vals = sorted({tuple(g.get(r) for r in row_groupby) for g in raw})
        idx = {(tuple(g.get(r) for r in row_groupby),
                tuple(g.get(c) for c in col_groupby)): g
               for g in raw}
        matrix = []
        for rv in row_vals:
            row = {"_row": dict(zip(row_groupby, rv))}
            for cv in col_vals:
                cell = idx.get((rv, cv))
                row[str(cv)] = {a: cell.get(a) for a in aggs} if cell else {}
            matrix.append(row)
        text_lines = [
            "=" * 60,
            _("Cross-tab: %(model)s", model=model),
            _("Rows: %s", ", ".join(row_groupby)),
            _("Columns: %s", ", ".join(col_groupby)),
            _("Cells: %s", ", ".join(aggs)),
            str(len(matrix)) + " rows × " + str(len(col_vals)) + " columns",
        ]
        return _result("\n".join(text_lines),
                       {"matrix": matrix, "col_values": [list(cv) for cv in col_vals],
                        "model": model})

    @staticmethod
    def _format_pivot_text(model: str, groupby: list, aggs: list, groups: list, has_more: bool) -> str:
        lines = ["=" * 60, _("Pivot: %s", model), "=" * 60,
                 _("Group by: %s", ", ".join(groupby)),
                 _("Aggregates: %s", ", ".join(aggs)), ""]
        for i, g in enumerate(groups, 1):
            parts = []
            for key in groupby:
                val = g.get(key)
                if isinstance(val, (list, tuple)) and len(val) == 2:
                    val = f"{val[1]} (ID: {val[0]})"
                elif val is False or val is None:
                    val = _("None")
                parts.append(f"{key}={val}")
            for key in aggs:
                parts.append(f"{key}={g.get(key)}")
            lines.append(f"[{i}] " + " | ".join(parts))
        if has_more:
            lines += ["", _("More groups available — use offset to paginate.")]
        return "\n".join(lines)

    # ── me ────────────────────────────────────────────────────────────

    @adv_tool(
        name="me",
        title="Me",
        description=(
            "Return current session identity: user, timezone, active company, "
            "permitted resources, and the OAuth scope active on this connection."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        operation=None,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    @api.model
    def me(self):
        from odoo.http import request as http_req
        base_text = build_user_context(self.env)
        resources = get_enabled_models(self.env)
        scope = getattr(http_req, "_adv_oauth_scope", None) if http_req else None
        structured = {
            "user": self.env.user.name,
            "uid": self.env.uid,
            "tz": self.env.user.tz or "UTC",
            "company": self.env.company.name,
            "permitted_resources": [r["model"] for r in resources],
            "active_scope": scope or "api_key",
        }
        structured.update(self._whoami_activity_context())
        extra = _(
            "\nPermitted resources: %(n)s models | Scope: %(scope)s",
            n=len(resources),
            scope=scope or "api_key",
        )
        return _result(base_text + extra, structured)

    def _whoami_activity_context(self) -> dict:
        ctx = {}
        try:
            ctx["pending_activities"] = self.env["mail.activity"].search_count(
                [("user_id", "=", self.env.uid),
                 ("date_deadline", "<=", fields.Date.context_today(self))]
            )
        except Exception:
            pass
        try:
            ctx["unread_messages"] = self.env["mail.notification"].search_count(
                [("res_partner_id", "=", self.env.user.partner_id.id),
                 ("is_read", "=", False)]
            )
        except Exception:
            pass
        return ctx

    # ── count ─────────────────────────────────────────────────────────────

    @adv_tool(
        name="count",
        title="Count",
        description=(
            "Count records matching an optional domain filter. "
            "Faster than query when you only need the total, not the records themselves."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Technical model name."},
                "domain": {
                    "type": ["array", "string", "null"],
                    "description": "Odoo domain filter. Null or [] = count all records.",
                },
            },
            "required": ["model"],
            "additionalProperties": False,
        },
        operation="read",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    @api.model
    def count(self, model, domain=None):
        rs = self._resolve_model(model)
        self._check_op(model, "read")
        filter_expr = self._coerce_domain(domain)
        total = rs.search_count(filter_expr)
        return _result(
            _("%(model)s: %(n)s records match", model=model, n=total),
            {"model": model, "count": total, "domain": filter_expr},
        )

    # ── explain ───────────────────────────────────────────────────────────

    @adv_tool(
        name="explain",
        title="Explain",
        description=(
            "Give a contextual summary of a record: its key fields, "
            "last chatter activity, current workflow state, and linked attachments. "
            "Useful for understanding a record's history and status at a glance."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Technical model name."},
                "record_id": {"type": "integer", "description": "Record ID."},
            },
            "required": ["model", "record_id"],
            "additionalProperties": False,
        },
        operation="read",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    @api.model
    def explain(self, model, record_id):
        rs = self._resolve_model(model)
        self._check_op(model, "read")
        record = self._browse_record_or_raise(model, rs, record_id)
        schema = rs.fields_get(attributes=["type", "string", "selection"])
        basic = record.with_context(bin_size=True).read(None)[0]
        state_info = self._state_summary(basic, schema)
        messages = self._recent_messages(model, int(record_id))
        attachments = self._attachment_summary(model, int(record_id))
        lines = [
            "=" * 60,
            _("%(model)s / ID %(id)s — %(name)s", model=model, id=record_id,
              name=basic.get("display_name") or str(record_id)),
            "=" * 60,
        ]
        if state_info:
            lines.append(_("State: %s", state_info))
        if attachments:
            lines.append(_("Attachments: %s", ", ".join(attachments)))
        if messages:
            lines.append("")
            lines.append(_("Recent activity:"))
            lines.extend(f"  • {m}" for m in messages)
        structured = {
            "model": model, "record_id": record_id,
            "display_name": basic.get("display_name"),
            "state": state_info, "attachments": attachments,
            "recent_messages": messages,
        }
        return _result("\n".join(lines), structured)

    def _state_summary(self, data: dict, schema: dict) -> str | None:
        for fname in ("state", "stage_id", "kanban_state"):
            val = data.get(fname)
            if val is None or val is False:
                continue
            meta = schema.get(fname) or {}
            if meta.get("type") == "selection" and meta.get("selection"):
                label = dict(meta["selection"]).get(val, val)
                return str(label)
            if isinstance(val, (list, tuple)) and len(val) == 2:
                return str(val[1])
            return str(val)
        return None

    def _recent_messages(self, model: str, record_id: int) -> list[str]:
        try:
            msgs = self.env["mail.message"].search(
                [("model", "=", model), ("res_id", "=", record_id),
                 ("message_type", "in", ["comment", "email"])],
                limit=5, order="id desc",
            )
            return [
                f"{m.date}: {m.author_id.name or '?'} — "
                + (m.subject or (m.body or "")[:60].replace("<br>", " ").strip())
                for m in msgs
            ]
        except (AccessError, Exception):
            return []

    def _attachment_summary(self, model: str, record_id: int) -> list[str]:
        try:
            atts = self.env["ir.attachment"].search(
                [("res_model", "=", model), ("res_id", "=", record_id)], limit=10
            )
            return [f"{a.name} ({a.mimetype or 'unknown'})" for a in atts]
        except (AccessError, Exception):
            return []

    # ── compare ──────────────────────────────────────────────────────────────

    @adv_tool(
        name="compare",
        title="Compare",
        description=(
            "Compare two records of the same model field by field. "
            "Returns only the fields where the values differ."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Technical model name."},
                "record_id_a": {"type": "integer", "description": "First record ID."},
                "record_id_b": {"type": "integer", "description": "Second record ID."},
                "fields": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Limit comparison to these fields. Null = all common fields.",
                },
            },
            "required": ["model", "record_id_a", "record_id_b"],
            "additionalProperties": False,
        },
        operation="read",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    @api.model
    def compare(self, model, record_id_a, record_id_b, fields=None):
        rs = self._resolve_model(model)
        self._check_op(model, "read")
        rec_a = self._browse_record_or_raise(model, rs, record_id_a)
        rec_b = self._browse_record_or_raise(model, rs, record_id_b)
        to_read = self._coerce_fields(fields)
        data_a = rec_a.with_context(bin_size=True).read(to_read)[0]
        data_b = rec_b.with_context(bin_size=True).read(to_read)[0]
        common = set(data_a) & set(data_b) - {"id"}
        diffs = {
            k: {"a": data_a[k], "b": data_b[k]}
            for k in sorted(common) if data_a[k] != data_b[k]
        }
        lines = [
            "=" * 60,
            _("Diff: %(model)s #%(a)s vs #%(b)s", model=model, a=record_id_a, b=record_id_b),
            _("Changed fields: %(n)s", n=len(diffs)),
            "=" * 60,
        ]
        for fname, vals in diffs.items():
            lines.append(f"{fname}:")
            lines.append(f"  A: {vals['a']}")
            lines.append(f"  B: {vals['b']}")
        if not diffs:
            lines.append(_("No differences found."))
        return _result("\n".join(lines), {"diffs": diffs, "model": model,
                                           "record_id_a": record_id_a, "record_id_b": record_id_b})

    # ── resources ───────────────────────────────────────────────────────────

    @adv_tool(
        name="resources",
        title="Resources",
        description=(
            "List available odoo:// resource URI templates for binary fields and "
            "attachments, usable with resources/read."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    @api.model
    def resources(self):
        resources = get_enabled_models(self.env)
        templates = [
            {
                "uri_template": "odoo://record/{model}/{id}/{field}",
                "description": _("Fetch a record's binary or image field."),
                "example": build_field_uri("res.partner", 10, "image_1920"),
            },
            {
                "uri_template": "odoo://attachment/{id}",
                "description": _("Fetch an ir.attachment by ID."),
                "example": build_attachment_uri(42),
            },
        ]
        lines = ["=" * 60, _("Resource catalog"), "=" * 60]
        for t in templates:
            lines.append(f"- {t['uri_template']}: {t['description']}")
        return _result("\n".join(lines), {
            "templates": templates,
            "accessible_resources": len(resources),
        })
