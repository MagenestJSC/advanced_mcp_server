from odoo import models


class IrActionsServer(models.Model):
    _inherit = "ir.actions.server"

    def _get_eval_context(self, action=None):
        ctx = super()._get_eval_context(action=action)
        tool_call = self.env.context.get("adv_tool_call")
        if isinstance(tool_call, dict):
            ctx["adv"] = tool_call
        return ctx
