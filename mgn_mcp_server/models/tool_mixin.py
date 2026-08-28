# Transport-agnostic Adv MCP tool layer.
# Tools are ordinary methods tagged with @adv_tool; contributions from
# _inherit = 'adv.tool.mixin' are discovered automatically via MRO scan.

import base64

from odoo import _, api, models
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.tools import ormcache
from odoo.tools.mimetypes import guess_mimetype

from ..server.helpers import (
    verify_model_operation as check_model_operation_allowed,
    is_model_accessible as is_model_amcp_enabled,
)
from ..tools.uri_schema import URIParseError, parse_attachment_uri, parse_field_uri

_TEXT_MIMETYPES = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/xml",
        "application/javascript",
        "application/ecmascript",
        "application/csv",
        "application/yaml",
        "application/x-yaml",
        "application/x-sh",
        "application/sql",
        "application/graphql",
        "image/svg+xml",
    }
)

_BINARY_FIELD_TYPES = ("binary", "image")


def adv_tool(name, description, input_schema, operation=None, title=None, **annotations):
    # Stamp a method as an adv_mcp tool by attaching its metadata dict.
    def decorator(method):
        method._adv_tool = {
            "name": name,
            "title": title,
            "description": description,
            "input_schema": input_schema,
            "operation": operation,
            "annotations": dict(annotations),
        }
        return method

    return decorator


class AdvToolMixin(models.AbstractModel):
    _name = "adv.tool.mixin"
    _description = "Adv MCP Tool Mixin"

    @api.model
    @ormcache(cache="stable")
    def _get_adv_tools(self):
        # Build tool index by scanning MRO for _adv_tool-tagged methods.
        index = {}
        visited_methods = set()
        for klass in type(self).mro():
            for attr_name, attr in vars(klass).items():
                if attr_name in visited_methods:
                    continue
                meta = getattr(attr, "_adv_tool", None)
                if meta is None:
                    continue
                visited_methods.add(attr_name)
                tool_name = meta["name"]
                if tool_name in index:
                    continue
                index[tool_name] = {
                    "method_name": attr_name,
                    "title": meta.get("title"),
                    "description": meta["description"],
                    "input_schema": meta["input_schema"],
                    "operation": meta["operation"],
                    "annotations": meta["annotations"],
                }
        return index

    def _resolve_model(self, model):
        # Validate model name and check adv_mcp access gate.
        if not model or model not in self.env:
            raise UserError(_("Unknown model: %s", model))
        if not is_model_amcp_enabled(self.env, model):
            raise AccessError(_("Model '%s' is not enabled for Adv MCP access.", model))
        # Force su=False so ORM always enforces ir.model.access + ir.rule,
        # regardless of any su=True inherited from the transaction env.
        return self.env(su=False)[model]

    def _check_op(self, model, operation):
        # Enforce per-operation adv_mcp gate.
        if not check_model_operation_allowed(self.env, model, operation):
            raise AccessError(
                _(
                    "Operation '%(operation)s' is not allowed on model "
                    "'%(model)s' via Adv MCP.",
                    operation=operation,
                    model=model,
                )
            )
        # Explicitly enforce Odoo native ACL — model-level ir.model.access check.
        self.env(su=False)[model].browse().check_access(operation)

    def _browse_record_or_raise(self, model, model_rs, record_id):
        # Browse a single record or raise MissingError.
        record = model_rs.browse(int(record_id)).exists()
        if not record:
            raise MissingError(
                _(
                    "Record not found: %(model)s with ID %(id)s",
                    model=model,
                    id=record_id,
                )
            )
        return record

    def _read_resource(self, uri):
        # Resolve an odoo:// URI to a content entry.
        if not isinstance(uri, str) or not uri.startswith("odoo://"):
            raise UserError(_("Invalid resource URI: %s", uri))
        try:
            ref = parse_field_uri(uri)
        except URIParseError:
            ref = None
        if ref is not None:
            return self._read_record_field(uri, ref)
        try:
            attachment_id = parse_attachment_uri(uri)
        except URIParseError as err:
            raise UserError(_("Unsupported resource URI: %s", uri)) from err
        return self._read_attachment(uri, attachment_id)

    def _read_record_field(self, uri, ref):
        # Read a binary/image field via odoo://record/... scheme.
        model_rs = self._resolve_model(ref.model)
        self._check_op(ref.model, "read")
        field = model_rs._fields.get(ref.field)
        if field is None:
            raise UserError(
                _(
                    "Unknown field '%(field)s' on model '%(model)s'.",
                    field=ref.field,
                    model=ref.model,
                )
            )
        if field.type not in _BINARY_FIELD_TYPES:
            raise UserError(
                _(
                    "Field '%(field)s' on '%(model)s' is not a binary field.",
                    field=ref.field,
                    model=ref.model,
                )
            )
        record = self._browse_record_or_raise(ref.model, model_rs, ref.record_id)
        value = record.read([ref.field])[0].get(ref.field)
        if not value:
            raise MissingError(
                _(
                    "Field '%(field)s' on %(model)s/%(id)s holds no data.",
                    field=ref.field,
                    model=ref.model,
                    id=ref.record_id,
                )
            )
        raw = base64.b64decode(value)
        mimetype = self._record_field_mimetype(ref.model, ref.record_id, ref.field, raw)
        return self._build_content_entry(uri, mimetype, raw)

    def _read_attachment(self, uri, attachment_id):
        # Read an ir.attachment via odoo://attachment/... scheme.
        attachment = self.env["ir.attachment"].browse(attachment_id).exists()
        if not attachment:
            raise MissingError(_("Attachment not found: %s", attachment_id))
        self._check_attachment_allowed(attachment)
        mimetype = attachment.mimetype
        raw = attachment.raw or b""
        if not mimetype:
            mimetype = guess_mimetype(raw, default="application/octet-stream")
        return self._build_content_entry(uri, mimetype, raw)

    def _check_attachment_allowed(self, attachment):
        # Enforce adv_mcp allow-list for attachment reads.
        if check_model_operation_allowed(self.env, "ir.attachment", "read"):
            return
        res_model = attachment.res_model
        if res_model and check_model_operation_allowed(self.env, res_model, "read"):
            return
        raise AccessError(
            _(
                "Attachment access via Adv MCP requires 'ir.attachment' or the "
                "attachment's parent model to be enabled for read."
            )
        )

    def _record_field_mimetype(self, model, record_id, field, raw):
        # Best-effort mimetype for a record binary field.
        att = self.env["ir.attachment"].search(
            [
                ("res_model", "=", model),
                ("res_id", "=", record_id),
                ("res_field", "=", field),
            ],
            limit=1,
        )
        if att and att.mimetype:
            return att.mimetype
        return guess_mimetype(raw, default="application/octet-stream")

    def _build_content_entry(self, uri, mimetype, raw):
        # Build one resources/read content entry from raw bytes.
        mimetype = mimetype or "application/octet-stream"
        if self._is_text_mimetype(mimetype):
            return {
                "uri": uri,
                "mimeType": mimetype,
                "text": raw.decode("utf-8", errors="replace"),
            }
        return {
            "uri": uri,
            "mimeType": mimetype,
            "blob": base64.b64encode(raw).decode("ascii"),
        }

    @staticmethod
    def _is_text_mimetype(mimetype):
        # Check whether mimetype indicates inline-able textual content.
        base = (mimetype or "").split(";", 1)[0].strip().lower()
        if not base:
            return False
        if base.startswith("text/"):
            return True
        if base in _TEXT_MIMETYPES:
            return True
        return base.endswith("+json") or base.endswith("+xml")
