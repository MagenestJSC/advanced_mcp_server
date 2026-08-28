from odoo import api, fields, models


class AdvModulePicker(models.TransientModel):
    _name = "adv.module.picker"
    _description = "Enable Modules for Adv MCP Access"

    @api.model
    def _available_modules(self):
        already_enabled = self.env["adv.module.access"].search([]).mapped("module_id.id")
        domain = [("state", "=", "installed")]
        if already_enabled:
            domain.append(("id", "not in", already_enabled))
        return domain

    module_ids = fields.Many2many(
        "ir.module.module",
        string="Modules",
        required=True,
        domain=lambda self: self._available_modules(),
    )
    allow_read = fields.Boolean(default=True)
    allow_create = fields.Boolean(default=False)
    allow_write = fields.Boolean(string="Allow Update", default=False)
    allow_unlink = fields.Boolean(string="Allow Delete", default=False)
    allow_method_calls = fields.Boolean(default=False)

    def action_confirm(self):
        self.ensure_one()
        vals = {
            "allow_read": self.allow_read,
            "allow_create": self.allow_create,
            "allow_write": self.allow_write,
            "allow_unlink": self.allow_unlink,
            "allow_method_calls": self.allow_method_calls,
        }
        existing = (
            self.env["adv.module.access"]
            .with_context(active_test=False)
            .search([("module_id", "in", self.module_ids.ids)])
        )
        by_module = {r.module_id.id: r for r in existing}
        new_vals = []
        for module in self.module_ids:
            rec = by_module.get(module.id)
            if not rec:
                new_vals.append({"module_id": module.id, **vals})
            elif not rec.active:
                rec.write({**vals, "active": True})
        if new_vals:
            self.env["adv.module.access"].create(new_vals)
        return {"type": "ir.actions.act_window_close"}
