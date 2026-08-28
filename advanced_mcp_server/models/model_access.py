from odoo import _, api, fields, models


class AdvModelAccess(models.Model):
    _name = "adv.model.access"
    _description = "Adv MCP Per-Model Permission Override"
    _rec_name = "model_id"

    module_access_id = fields.Many2one(
        "adv.module.access",
        required=True,
        ondelete="cascade",
        index=True,
    )
    model_id = fields.Many2one(
        "ir.model",
        string="Model",
        required=True,
        ondelete="cascade",
        domain=[("transient", "=", False)],
    )
    model_name = fields.Char(
        related="model_id.model", string="Technical Name", store=True, readonly=True
    )
    allow_read = fields.Boolean(default=True)
    allow_create = fields.Boolean(default=True)
    allow_write = fields.Boolean(string="Allow Update", default=True)
    allow_unlink = fields.Boolean(string="Allow Delete", default=True)
    allow_method_calls = fields.Boolean(default=True)

    _unique_model = models.Constraint(
        "UNIQUE(module_access_id, model_id)",
        "A model can only be overridden once per module access record.",
    )

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        self.env["adv.module.access"]._clear_caches()
        return records

    def write(self, vals):
        result = super().write(vals)
        self.env["adv.module.access"]._clear_caches()
        return result

    def unlink(self):
        result = super().unlink()
        self.env["adv.module.access"]._clear_caches()
        return result
