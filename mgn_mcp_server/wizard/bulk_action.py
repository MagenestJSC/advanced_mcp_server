from odoo import fields, models


class AdvBulkAction(models.TransientModel):
    _name = "adv.bulk.action"
    _description = "Confirm Bulk OAuth Action"

    operation = fields.Selection(
        [("revoke", "Revoke Tokens"), ("deactivate", "Deactivate Clients")],
        required=True,
    )

    def action_confirm(self):
        self.ensure_one()
        ids = self.env.context.get("active_ids", [])
        if self.operation == "revoke":
            self.env["adv.oauth.token"].browse(ids).action_revoke()
        else:
            self.env["adv.oauth.client"].browse(ids).action_deactivate()
        return {"type": "ir.actions.act_window_close"}
