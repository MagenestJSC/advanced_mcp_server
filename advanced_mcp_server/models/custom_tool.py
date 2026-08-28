# Admin-defined Adv MCP tools backed by ir.actions.server.

import json
import logging
import re

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

_logger = logging.getLogger(__name__)

_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class AdvCustomTool(models.Model):
    _name = "adv.custom.tool"
    _description = "Adv MCP Custom Tool"

    name = fields.Char(
        required=True,
        help="Tool name the LLM calls. Must match ^[A-Za-z0-9_-]{1,64}$ and not "
        "collide with a builtin tool name.",
    )
    description = fields.Text(
        required=True,
        help="LLM contract: describe what the tool does, when to call it, and each argument.",
    )
    action_id = fields.Many2one(
        "ir.actions.server",
        string="Server Action",
        required=True,
        ondelete="cascade",
    )
    input_schema = fields.Text(
        string="Input Schema",
        default='{"type": "object", "properties": {}}',
        help="JSON Schema advertised in tools/list. Action reads args from adv['args'].",
    )
    is_readonly = fields.Boolean(
        string="Read-only",
        default=False,
        help="Advertised as readOnlyHint. Read-only OAuth sessions may only call these.",
    )
    active = fields.Boolean(default=True)

    _unique_name = models.Constraint(
        "UNIQUE(name)",
        "A custom tool with this name already exists.",
    )

    @api.constrains("name")
    def _check_name(self):
        builtins = self.env["adv.tool.mixin"]._get_adv_tools()
        for tool in self:
            if not _TOOL_NAME_RE.fullmatch(tool.name or ""):
                raise ValidationError(
                    _(
                        "Invalid tool name '%(name)s': use 1-64 characters from "
                        "A-Z, a-z, 0-9, underscore or hyphen.",
                        name=tool.name,
                    )
                )
            if tool.name in builtins:
                raise ValidationError(
                    _(
                        "'%(name)s' is the name of a builtin tool; pick a different name.",
                        name=tool.name,
                    )
                )

    @api.constrains("input_schema")
    def _check_input_schema(self):
        for tool in self:
            try:
                schema = json.loads(tool.input_schema or "")
            except (ValueError, TypeError) as err:
                raise ValidationError(
                    _("Input schema must be valid JSON: %s", err)
                ) from err
            if not isinstance(schema, dict):
                raise ValidationError(
                    _(
                        "Input schema must be a JSON object, e.g. "
                        '{"type": "object", "properties": {}}.'
                    )
                )
            schema_type = schema.get("type")
            if schema_type is not None and schema_type != "object":
                raise ValidationError(
                    _(
                        'Input schema \'type\' must be "object", got "%(type)s".',
                        type=schema_type,
                    )
                )
            required = schema.get("required")
            if required is not None and (
                not isinstance(required, list)
                or not all(isinstance(item, str) for item in required)
            ):
                raise ValidationError(
                    _("Input schema 'required' must be a list of property name strings.")
                )

    @api.constrains("is_readonly")
    def _check_is_readonly_system_only(self):
        for tool in self:
            if tool.is_readonly and not self.env.user.has_group("base.group_system"):
                raise ValidationError(
                    _(
                        "Only a system administrator may mark a custom tool read-only, "
                        "because that flag governs the OAuth read/write scope boundary."
                    )
                )

    @api.constrains("action_id")
    def _check_action_is_code(self):
        for tool in self:
            try:
                tool.action_id.with_user(self.env.user).check_access("read")
            except AccessError:
                raise ValidationError(
                    _("You do not have access to the selected server action.")
                ) from None
            action = tool.action_id.sudo()
            if action.state != "code":
                raise ValidationError(
                    _(
                        "Custom tools must wrap a Python Code server action. Other "
                        "action types run once per selected record, and a tool call "
                        "passes no record, so the action would silently do nothing."
                    )
                )

    def _sudo_action(self):
        self.ensure_one()
        return self.sudo().action_id

    def _user_can_run(self):
        # Check whether the calling user may run this tool.
        self.ensure_one()
        action = self._sudo_action()
        groups = action.group_ids
        if groups:
            return bool(groups & self.env.user.all_group_ids)
        model = action.model_id.model
        if not model or model not in self.env:
            return False
        return self.env[model].has_access("write")

    def _run_tool(self, arguments):
        # Execute the wrapped action as the calling user and return its result.
        self.ensure_one()
        if not self._user_can_run():
            raise AccessError(
                _("You are not allowed to run the '%(name)s' tool.", name=self.name)
            )
        action_su = self._sudo_action()
        if action_su.state != "code":
            raise UserError(
                _("This custom tool's server action is not a Python Code action.")
            )
        shared = {"args": arguments or {}, "result": None}
        action_su.with_env(self.env).with_context(adv_tool_call=shared).run()
        return shared["result"]

    def _parsed_input_schema(self):
        self.ensure_one()
        try:
            parsed = json.loads(self.input_schema or "{}")
        except ValueError as err:
            _logger.warning("Custom tool %s: malformed input_schema: %s", self.name, err)
            return None
        if not isinstance(parsed, dict):
            _logger.warning("Custom tool %s: input_schema is not a JSON object", self.name)
            return None
        return parsed

    def _visible_tools(self):
        # Return active custom tools the calling user may run.
        tools = self.sudo().search([("active", "=", True)]).with_env(self.env)
        return tools.filtered(lambda t: t._user_can_run())
