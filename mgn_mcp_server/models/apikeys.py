# Lets users mint adv-scoped API keys from the standard wizard.

from odoo import fields, models

from odoo.addons.base.models.res_users import check_identity


class ResUsersApikeysDescription(models.TransientModel):
    _inherit = "res.users.apikeys.description"

    scope_mode = fields.Selection(
        selection=[
            ("global", "All APIs (default)"),
            ("adv", "AMCP-only scope"),
        ],
        string="Access",
        default="global",
        required=True,
        help="Choose what this key can do.\n"
        "- All APIs (default): a normal Odoo API key with full RPC access.\n"
        "- AMCP-only scope: the key authenticates ONLY on the Adv MCP endpoint. "
        "It cannot be used for general RPC.",
    )

    @check_identity
    def make_key(self):
        if self.scope_mode == "adv":
            self = self.with_context(adv_api_key_scope="adv")
        return super().make_key()


class ResUsersApikeys(models.Model):
    _inherit = "res.users.apikeys"

    def _generate(self, scope, name, expiration_date):
        scope = scope or self.env.context.get("adv_api_key_scope")
        return super()._generate(scope, name, expiration_date)
