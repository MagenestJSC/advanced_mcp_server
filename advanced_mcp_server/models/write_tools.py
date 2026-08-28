



# Write-side capabilities for adv.tool.mixin:
# add, edit, drop, run, attach, pipeline.
# post_message is merged into run (method="message_post").

import base64
import json
import logging
import re

from odoo import _, api, models
from odoo.exceptions import AccessError, MissingError, UserError

from ..server.helpers import (
    verify_model_method as check_model_method_allowed,
    verify_model_operation as check_model_operation_allowed,
    map_method_to_operation,
)
from .tool_mixin import adv_tool
from .read_tools import MAX_LIMIT, _result

_logger = logging.getLogger(__name__)

_ESSENTIAL_FIELDS = ["id", "display_name"]

_CHATTER_SUBTYPES = {"note": "mail.mt_note", "comment": "mail.mt_comment"}

_ORM_METHOD_BLOCKLIST = frozenset(
    {
        "create", "write", "unlink", "read", "search", "search_read",
        "search_count", "search_fetch", "fetch", "read_group",
        "formatted_read_group", "formatted_read_grouping_sets",
        "read_progress_bar", "name_search", "search_panel_select_range",
        "search_panel_select_multi_range", "copy", "browse",
        "_write", "sudo", "with_user", "with_env", "with_context",
        "fields_get", "load", "export_data", "name_create", "run",
        "method_direct_trigger",
    }
)

_RESTRICTED_MODEL_PREFIXES = ("ir.actions", "ir.cron")

_BATCH_ALLOWED = frozenset({"add", "edit", "drop", "run"})
_BATCH_MAX_OPS = 20
_REF_RE = re.compile(r"\{\{([^.}]+)\.([^}]+)\}\}")


def _coerce_json(value, max_records: int = MAX_LIMIT):
    if isinstance(value, models.BaseModel):
        capped = value[:max_records]
        try:
            out = [{"id": r.id, "display_name": r.display_name} for r in capped]
        except Exception:
            _logger.debug("run: display_name unavailable for %s", value._name)
            out = list(capped.ids)
        if len(value) > max_records:
            out.append(_(
                "... [truncated: %(shown)d of %(total)d]",
                shown=max_records, total=len(value),
            ))
        return out
    if isinstance(value, dict):
        return {str(k): _coerce_json(v, max_records) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_coerce_json(item, max_records) for item in value]
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    return str(value)


class AdvMutationCapabilities(models.AbstractModel):
    _inherit = "adv.tool.mixin"

    def _web_url(self, model: str, record_id: int) -> str:
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url")
        return f"{base}/odoo/{model}/{record_id}" if base else ""

    def _confirmation(self, model: str, record, message: str) -> dict:
        data = record.read(_ESSENTIAL_FIELDS)[0]
        url = self._web_url(model, record.id)
        lines = [message]
        if data.get("display_name"):
            lines.append(_("Name: %s", data["display_name"]))
        if url:
            lines.append(_("URL: %s", url))
        return _result("\n".join(lines), {"success": True, "record": data, "url": url})

    # ── add ────────────────────────────────────────────────────────────

    @adv_tool(
        name="add",
        title="Add",
        description=(
            "Create a new record. Pass 'values' as field→value pairs. "
            "Set dry_run=true to validate inputs and preview what would be created "
            "without actually saving to the database."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Technical model name."},
                "values": {
                    "type": "object",
                    "description": 'Field values, e.g. {"name": "ACME Corp"}.',
                },
                "dry_run": {
                    "type": "boolean",
                    "default": False,
                    "description": "Validate without saving. Returns what would be created.",
                },
            },
            "required": ["model", "values"],
            "additionalProperties": False,
        },
        operation="create",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
    @api.model
    def add(self, model, values, dry_run=False):
        rs = self._resolve_model(model)
        self._check_op(model, "create")
        if not isinstance(values, dict) or not values:
            raise UserError(_("'values' must be a non-empty object."))
        if dry_run:
            return self._dry_run_add(rs, model, values)
        record = rs.create(values)
        return self._confirmation(
            model, record,
            _("Created %(model)s record with ID %(id)s", model=model, id=record.id),
        )

    def _dry_run_add(self, rs, model: str, values: dict) -> dict:
        schema = rs.fields_get(attributes=["type", "string", "required"])
        missing = [
            f for f, m in schema.items()
            if m.get("required") and f not in values and f not in ("id", "create_uid")
        ]
        preview = {k: v for k, v in values.items() if k in schema}
        unknown = [k for k in values if k not in schema]
        text_lines = [_("Dry run — record would NOT be saved"), _("Model: %s", model)]
        if missing:
            text_lines.append(_("Missing required fields: %s", ", ".join(missing)))
        if unknown:
            text_lines.append(_("Unknown fields (ignored): %s", ", ".join(unknown)))
        text_lines.append(_("Preview: %s", json.dumps(preview, default=str)))
        return _result("\n".join(text_lines), {
            "dry_run": True, "model": model, "preview": preview,
            "missing_required": missing, "unknown_fields": unknown,
        })

    # ── edit ─────────────────────────────────────────────────────────────

    @adv_tool(
        name="edit",
        title="Edit",
        description=(
            "Update an existing record. Pass only the fields that need to change — "
            "unchanged fields are left untouched. Runs as the calling user."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Technical model name."},
                "record_id": {"type": "integer", "description": "Record ID to update."},
                "values": {
                    "type": "object",
                    "description": 'Changed field values, e.g. {"name": "New name"}.',
                },
            },
            "required": ["model", "record_id", "values"],
            "additionalProperties": False,
        },
        operation="write",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    )
    @api.model
    def edit(self, model, record_id, values):
        rs = self._resolve_model(model)
        self._check_op(model, "write")
        if not isinstance(values, dict) or not values:
            raise UserError(_("'values' must be a non-empty object."))
        record = self._browse_record_or_raise(model, rs, record_id)
        record.write(values)
        return self._confirmation(
            model, record,
            _("Updated %(model)s record with ID %(id)s", model=model, id=record.id),
        )

    # ── drop ────────────────────────────────────────────────────────────

    @adv_tool(
        name="drop",
        title="Drop",
        description=(
            "Delete a record permanently. Returns a list of related records that "
            "may be affected by cascade deletion. This action cannot be undone."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Technical model name."},
                "record_id": {"type": "integer", "description": "Record ID to delete."},
            },
            "required": ["model", "record_id"],
            "additionalProperties": False,
        },
        operation="unlink",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )
    @api.model
    def drop(self, model, record_id):
        rs = self._resolve_model(model)
        self._check_op(model, "unlink")
        record = self._browse_record_or_raise(model, rs, record_id)
        deleted_id = record.id
        deleted_name = record.display_name or _("ID %s", deleted_id)
        affected = self._find_affected_relations(model, deleted_id)
        record.unlink()
        msg = _(
            "Removed %(model)s '%(name)s' (ID: %(id)s)",
            model=model, name=deleted_name, id=deleted_id,
        )
        return _result(msg, {
            "success": True, "deleted_id": deleted_id, "deleted_name": deleted_name,
            "affected_relations": affected,
        })

    def _find_affected_relations(self, model: str, record_id: int) -> list[dict]:
        affected = []
        for field in self.env[model]._fields.values():
            if getattr(field, "ondelete", None) != "cascade":
                continue
            rel_model = getattr(field, "inverse_name", None) and field.comodel_name
            if not rel_model or rel_model not in self.env:
                continue
            try:
                count = self.env[rel_model].search_count(
                    [(field.name if hasattr(field, "name") else "id", "=", record_id)]
                )
                if count:
                    affected.append({"model": rel_model, "count": count})
            except Exception:
                pass
        return affected

    # ── run ────────────────────────────────────────────────────────────

    @adv_tool(
        name="run",
        title="Run",
        description=(
            "Call a public business method on a model. "
            "Also handles chatter posting: use method='message_post' with "
            "'kwargs' containing body, subject, subtype ('note' or 'comment'). "
            "Blocked for ORM methods and private (underscore-prefixed) names. "
            "Requires 'allow_method_calls' to be enabled for the model."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "Technical model name."},
                "method": {
                    "type": "string",
                    "description": "Public method name. Use 'message_post' to post to the chatter.",
                },
                "record_ids": {
                    "type": ["array", "null"],
                    "items": {"type": "integer"},
                    "description": "Record IDs to invoke on. Omit for model-level methods.",
                },
                "args": {"type": ["array", "null"], "description": "Positional arguments."},
                "kwargs": {"type": ["object", "null"], "description": "Keyword arguments."},
            },
            "required": ["model", "method"],
            "additionalProperties": False,
        },
        operation=None,
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=True,
    )
    @api.model
    def run(self, model, method, record_ids=None, args=None, kwargs=None):
        rs = self._resolve_model(model)
        method = (method or "").strip()
        if not method:
            raise UserError(_("No method name provided."))
        if method in ("message_post", "post_message"):
            return self._post_to_chatter(model, rs, record_ids, kwargs or {})
        if not check_model_method_allowed(self.env, model):
            raise AccessError(_("Method calls are not enabled for model '%s'.", model))
        self._validate_invocation(model, rs, method)
        target, record_ids = self._resolve_invocation_target(rs, model, record_ids)
        pos_args = list(args or [])
        kw_args = dict(kwargs or {})
        if not isinstance(pos_args, list):
            raise UserError(_("'args' must be a list."))
        if not isinstance(kw_args, dict):
            raise UserError(_("'kwargs' must be an object."))
        _logger.info("invoke: model=%s method=%s records=%s", model, method,
                     len(target) if record_ids else 0)
        raw = getattr(target, method)(*pos_args, **kw_args)
        serialized = _coerce_json(raw)
        msg = _("Called %(model)s.%(method)s", model=model, method=method)
        try:
            rendered = json.dumps(serialized, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            rendered = str(serialized)
        return _result(f"{msg}\n{_('Result: %s', rendered)}",
                       {"success": True, "result": serialized})

    def _post_to_chatter(self, model: str, rs, record_ids, kw: dict) -> dict:
        self._check_op(model, "write")
        body = kw.get("body") or kw.get("message")
        if not isinstance(body, str) or not body.strip():
            raise UserError(_("'kwargs.body' is required for message_post."))
        if not hasattr(rs, "message_post"):
            raise UserError(_("Model '%s' does not support chatter.", model))
        ids = record_ids if isinstance(record_ids, list) and record_ids else None
        if not ids:
            raise UserError(_("'record_ids' is required for message_post."))
        record = self._browse_record_or_raise(model, rs, ids[0])
        subtype = (kw.get("subtype") or "note").strip().lower()
        if subtype not in _CHATTER_SUBTYPES:
            raise UserError(_("subtype must be 'note' or 'comment'."))
        post_kw = {
            "body": body,
            "message_type": kw.get("message_type", "comment"),
            "subtype_xmlid": _CHATTER_SUBTYPES[subtype],
        }
        for opt in ("subject", "partner_ids", "attachment_ids"):
            if kw.get(opt) is not None:
                post_kw[opt] = kw[opt]
        msg_rec = record.message_post(**post_kw)
        confirmation = _(
            "Posted message %(mid)s to %(model)s record %(rid)s",
            mid=msg_rec.id, model=model, rid=ids[0],
        )
        return _result(confirmation, {"success": True, "message_id": msg_rec.id})

    def _validate_invocation(self, model: str, rs, method: str):
        if method.startswith("_"):
            raise UserError(_("Private methods cannot be invoked via this gateway: %s", method))
        if any(model == p or model.startswith(p + ".") for p in _RESTRICTED_MODEL_PREFIXES):
            raise AccessError(_("Method calls on '%s' are restricted.", model))
        if method.startswith("web_") or method in _ORM_METHOD_BLOCKLIST:
            raise AccessError(
                _("'%(m)s' is an ORM/web method; use the dedicated CRUD tools.", m=method)
            )
        mapped_op = map_method_to_operation(method)
        if mapped_op and not check_model_operation_allowed(self.env, model, mapped_op):
            raise AccessError(
                _("Method '%(m)s' maps to operation '%(op)s' which is not enabled for '%(model)s'.",
                  m=method, op=mapped_op, model=model)
            )
        if not mapped_op and (hasattr(models.BaseModel, method) or
                               hasattr(self.env["base"], method)):
            raise AccessError(_("'%s' is a generic ORM method; use the dedicated tools.", method))
        if not callable(getattr(rs, method, None)):
            raise UserError(_("'%(m)s' is not a callable method on '%(model)s'.",
                               m=method, model=model))

    def _resolve_invocation_target(self, rs, model: str, record_ids):
        if not record_ids:
            return rs, None
        if not isinstance(record_ids, list):
            raise UserError(_("'record_ids' must be a list."))
        max_ids = self._cfg_int("adv_mcp.max_limit", MAX_LIMIT) or MAX_LIMIT
        if len(record_ids) > max_ids:
            raise UserError(_(
                "Too many record_ids: %(n)s (max %(max)s).", n=len(record_ids), max=max_ids
            ))
        try:
            record_ids = [int(rid) for rid in record_ids]
        except (TypeError, ValueError) as err:
            raise UserError(_("'record_ids' must be a list of integers.")) from err
        target = rs.browse(record_ids).exists()
        missing = [rid for rid in record_ids if rid not in set(target.ids)]
        if missing:
            raise MissingError(_(
                "Records not found: %(model)s IDs %(ids)s", model=model, ids=missing
            ))
        return target, record_ids

    # ── attach ────────────────────────────────────────────────────────────

    @adv_tool(
        name="attach",
        title="Attach",
        description=(
            "Upload a file to Odoo as an ir.attachment. "
            "Pass 'content' as a base64-encoded string and 'filename' as the file name. "
            "Optionally link to a record with 'res_model' and 'res_id'. "
            "Returns an odoo:// URI for the created attachment."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filename": {"type": "string", "description": "File name including extension."},
                "content": {"type": "string", "description": "Base64-encoded file content."},
                "mimetype": {"type": ["string", "null"], "description": "MIME type. Detected if omitted."},
                "res_model": {"type": ["string", "null"], "description": "Model to link attachment to."},
                "res_id": {"type": ["integer", "null"], "description": "Record ID to link to."},
            },
            "required": ["filename", "content"],
            "additionalProperties": False,
        },
        operation="create",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
    @api.model
    def attach(self, filename, content, mimetype=None, res_model=None, res_id=None):
        self._check_upload_allowed(res_model)
        try:
            size = len(base64.b64decode(content))
        except Exception as err:
            raise UserError(_("'content' must be valid base64.")) from err
        vals = {"name": filename, "datas": content}
        if mimetype:
            vals["mimetype"] = mimetype
        if res_model:
            vals["res_model"] = res_model
        if res_id is not None:
            vals["res_id"] = int(res_id)
        att = self.env["ir.attachment"].create(vals)
        uri = f"odoo://attachment/{att.id}"
        msg = _("Uploaded '%(name)s' as attachment %(id)s (%(size)s bytes)",
                name=filename, id=att.id, size=size)
        return _result(f"{msg}\n{uri}",
                       {"success": True, "attachment_id": att.id, "uri": uri,
                        "filename": filename, "size": size})

    def _check_upload_allowed(self, res_model=None):
        if check_model_operation_allowed(self.env, "ir.attachment", "create"):
            return
        if res_model and check_model_operation_allowed(self.env, res_model, "create"):
            return
        raise AccessError(_(
            "Upload requires 'ir.attachment' or the target model to be enabled for create."
        ))

    # ── pipeline ─────────────────────────────────────────────────────────────

    @adv_tool(
        name="pipeline",
        title="Pipeline",
        description=(
            "Execute multiple write operations atomically. "
            "All operations commit together or all roll back on the first failure. "
            "Reference an earlier operation's result with {{op_id.field}} in any string arg. "
            "Allowed tools: add, edit, drop, run. Max %(max)s operations."
        ) % {"max": _BATCH_MAX_OPS},
        input_schema={
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "maxItems": _BATCH_MAX_OPS,
                    "description": "List of operations to execute in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": ["string", "null"],
                                   "description": "Optional name for referencing this result."},
                            "tool": {"type": "string", "enum": list(_BATCH_ALLOWED)},
                            "args": {"type": "object",
                                     "description": "Arguments for the tool. Supports {{id.field}} templates."},
                        },
                        "required": ["tool", "args"],
                    },
                },
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
        operation="create",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    )
    @api.model
    def pipeline(self, operations):
        if not isinstance(operations, list) or not operations:
            raise UserError(_("'operations' must be a non-empty list."))
        if len(operations) > _BATCH_MAX_OPS:
            raise UserError(_("Too many operations: %(n)s (max %(max)s).",
                               n=len(operations), max=_BATCH_MAX_OPS))
        results = []
        named = {}
        with self.env.cr.savepoint():
            for i, op in enumerate(operations):
                result = self._run_batch_op(op, named, i)
                op_id = op.get("id")
                if op_id:
                    named[op_id] = result
                results.append({"id": op_id, "result": result})
        return _result(
            _("Batch completed: %(n)s operations", n=len(results)),
            {"results": results, "count": len(results)},
        )

    def _run_batch_op(self, op, named, index):
        tool = op.get("tool")
        if tool not in _BATCH_ALLOWED:
            raise UserError(_("Op %(i)s: '%(t)s' not allowed in batch.", i=index, t=tool))
        args = self._resolve_refs(op.get("args") or {}, named, index)
        if not isinstance(args, dict):
            raise UserError(_("Op %(i)s: 'args' must be an object.", i=index))
        return getattr(self, tool)(**args)

    def _resolve_refs(self, obj, named, index):
        if isinstance(obj, str):
            m = re.fullmatch(r"\{\{([^.}]+)\.([^}]+)\}\}", obj)
            if m:
                return self._extract_ref(m.group(1), m.group(2), named, index)
            return _REF_RE.sub(
                lambda m: str(self._extract_ref(m.group(1), m.group(2), named, index)), obj
            )
        if isinstance(obj, dict):
            return {k: self._resolve_refs(v, named, index) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._resolve_refs(v, named, index) for v in obj]
        return obj

    def _extract_ref(self, ref_name, field, named, index):
        result = named.get(ref_name)
        if result is None:
            raise UserError(_("Op %(i)s: unknown ref '%(r)s'.", i=index, r=ref_name))
        sc = result.get("structuredContent", {})
        data = sc.get("record", sc)
        if field not in data:
            raise UserError(_("Op %(i)s: '%(f)s' not found in ref '%(r)s'.",
                               i=index, f=field, r=ref_name))
        return data[field]
