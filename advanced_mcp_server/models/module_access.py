from odoo import _, api, fields, models, tools
from odoo.exceptions import ValidationError


class AdvModuleAccess(models.Model):
    _name = "adv.module.access"
    _description = "Adv MCP Module Access"
    _rec_name = "module_id"

    module_id = fields.Many2one(
        "ir.module.module",
        string="Module",
        required=True,
        index=True,
        ondelete="cascade",
        domain=[("state", "=", "installed"), ("name", "not in", ["base", "base_setup"])],
    )
    module_name = fields.Char(
        related="module_id.name", string="Technical Name", store=True, readonly=True
    )
    active = fields.Boolean(default=True)
    allow_read = fields.Boolean(default=True)
    allow_create = fields.Boolean(default=False)
    allow_write = fields.Boolean(string="Allow Update", default=False)
    allow_unlink = fields.Boolean(string="Allow Delete", default=False)
    allow_method_calls = fields.Boolean(
        default=False,
        help="Allow clients to call public business methods via invoke.",
    )
    model_ids = fields.Many2many("ir.model", string="Models", compute="_compute_model_ids")
    model_access_ids = fields.One2many(
        "adv.model.access", "module_access_id", string="Per-Model Overrides"
    )
    notes = fields.Text()

    _unique_module = models.Constraint(
        "UNIQUE(module_id)",
        "A module can only be registered once.",
    )

    def _compute_model_ids(self):
        all_models = self.env["ir.model"].sudo().search([("transient", "=", False)])
        for rec in self:
            name = rec.module_name
            if not name:
                rec.model_ids = self.env["ir.model"]
                continue
            rec.model_ids = all_models.filtered(
                lambda m, n=name: n in [s.strip() for s in (m.modules or "").split(",") if s.strip()]
            )

    # --- cache invalidation ---

    def _clear_caches(self):
        self.env.registry.clear_cache()

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records:
            rec._sync_model_access_ids()
        self._clear_caches()
        return records

    def write(self, vals):
        result = super().write(vals)
        if "module_id" in vals:
            for rec in self:
                rec._sync_model_access_ids()
        self._clear_caches()
        return result

    def unlink(self):
        result = super().unlink()
        self._clear_caches()
        return result

    def action_sync_models(self):
        for rec in self:
            rec._sync_model_access_ids()
        self._clear_caches()

    def _sync_model_access_ids(self):
        """Add rows for new models and remove rows for models no longer in the module."""
        new_model_ids = set(self.model_ids.ids)
        existing = {ma.model_id.id: ma for ma in self.model_access_ids}
        existing_ids = set(existing.keys())

        to_add = new_model_ids - existing_ids
        to_remove = existing_ids - new_model_ids

        if to_add:
            self.env["adv.model.access"].sudo().create(
                [{"module_access_id": self.id, "model_id": mid} for mid in to_add]
            )
        if to_remove:
            self.env["adv.model.access"].sudo().browse(
                [existing[mid].id for mid in to_remove]
            ).unlink()

    # --- cached config readers ---

    @api.model
    @tools.ormcache()
    def _get_amcp_enabled(self):
        return self.env["adv.server.config"].sudo()._get_config().enabled

    @api.model
    @tools.ormcache()
    def _get_oauth_enabled(self):
        return self.env["adv.server.config"].sudo()._get_config().enable_oauth

    @api.model
    @tools.ormcache()
    def _get_allowed_origins(self):
        return self.env["adv.server.config"].sudo()._get_config().allowed_origin_set()

    # --- model-level lookups ---

    def _module_names_for_model(self, model_name) -> list[str]:
        ir_model = self.env["ir.model"].sudo().search([("model", "=", model_name)], limit=1)
        if not ir_model:
            return []
        return [s.strip() for s in (ir_model.modules or "").split(",") if s.strip()]

    def _find_enabled_modules(self, model_name):
        """Return ALL active adv.module.access records that cover model_name.

        A model can belong to multiple Odoo modules via _inherit (e.g. sale.order
        lives in both "sale" and "sale_management"). We return every registered
        module that matches so callers can apply OR semantics across them.
        """
        names = self._module_names_for_model(model_name)
        if not names:
            return self.browse()
        return self.sudo().search([("module_name", "in", names), ("active", "=", True)])

    @api.model
    @tools.ormcache("model_name")
    def is_model_enabled(self, model_name):
        # A model is enabled only if it has an explicit override row in at least
        # one covering active module (opt-in: deleting a row = removing access).
        recs = self._find_enabled_modules(model_name)
        if not recs:
            return False
        model_ir = self._model_ir(model_name)
        return any(bool(self._get_model_override(rec, model_ir)) for rec in recs)

    def _get_model_override(self, module_rec, model_ir):
        if not model_ir:
            return self.browse()
        return self.env["adv.model.access"].sudo().search(
            [("module_access_id", "=", module_rec.id), ("model_id", "=", model_ir.id)],
            limit=1,
        )

    def _model_ir(self, model_name):
        return self.env["ir.model"].sudo().search([("model", "=", model_name)], limit=1)

    @api.model
    @tools.ormcache("model_name", "operation")
    def check_model_operation_enabled(self, model_name, operation):
        if operation not in ["read", "create", "write", "unlink"]:
            raise ValidationError(_("Invalid operation: %(operation)s", operation=operation))
        recs = self._find_enabled_modules(model_name)
        model_ir = self._model_ir(model_name)
        for rec in recs:
            if not rec["allow_" + operation]:
                continue
            override = self._get_model_override(rec, model_ir)
            # No override row = model was removed from the allow-list → deny.
            if not override or not override["allow_" + operation]:
                continue
            return True
        return False

    @api.model
    @tools.ormcache("model_name")
    def is_method_call_enabled(self, model_name):
        recs = self._find_enabled_modules(model_name)
        model_ir = self._model_ir(model_name)
        for rec in recs:
            if not rec.allow_method_calls:
                continue
            override = self._get_model_override(rec, model_ir)
            if not override or not override.allow_method_calls:
                continue
            return True
        return False
