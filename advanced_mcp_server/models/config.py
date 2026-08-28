from odoo import api, fields, models
from odoo.exceptions import ValidationError

class AdvServerConfig(models.Model):
    """Singleton configuration record for the Advanced MCP Server gateway."""

    _name = "adv.server.config"
    _description = "Adv MCP Gateway Configuration"

    enabled = fields.Boolean(
        string="Gateway Enabled",
        help="Master switch — disables the entire /mcp_server endpoint when off.",
        default=False,
    )
    enable_oauth = fields.Boolean(
        string="OAuth 2.1",
        help="Enable the built-in OAuth 2.1 authorization flow for browser clients.",
        default=True,
    )
    enable_rate_limiting = fields.Boolean(
        string="Rate Limiting",
        default=False,
    )
    request_limit = fields.Integer(
        string="Requests / Minute (per user)",
        help="Per-user rate cap. Ignored when rate limiting is off.",
        default=300,
    )
    admin_request_limit = fields.Integer(
        string="Admin Requests / Minute",
        help="Higher rate cap for gateway admin users. 0 = same as regular limit.",
        default=0,
    )
    enable_logging = fields.Boolean(
        string="Event Logging",
        default=True,
    )
    log_retention_days = fields.Integer(
        string="Log Retention (days)",
        help="0 = keep forever.",
        default=30,
    )
    default_limit = fields.Integer(string="Default Record Limit", default=10)
    max_limit = fields.Integer(string="Max Record Limit", default=100)
    max_smart_fields = fields.Integer(string="Max Smart Fields", default=15)
    max_related_items = fields.Integer(string="Max Related Items (fetch)", default=3)
    allowed_origins = fields.Char(
        string="Allowed Origins",
        help="Comma-separated browser Origins. Empty = unrestricted.",
    )

    @api.model
    def _get_config(self):
        return self.env.ref("advanced_mcp_server.adv_server_config_default")

    @api.model_create_multi
    def create(self, vals_list):
        if self.search_count([]) > 0:
            raise ValidationError(
                "Only one Gateway Configuration record is allowed. Edit the existing one."
            )
        return super().create(vals_list)

    def unlink(self):
        if not self.env.context.get("force_unlink"):
            raise ValidationError("The Gateway Configuration record cannot be deleted.")
        return super().unlink()

    def write(self, vals):
        result = super().write(vals)
        self.env.registry.clear_cache()
        return result

    def allowed_origin_set(self) -> tuple:
        raw = (self.allowed_origins or "").strip()
        return tuple(
            part.strip().rstrip("/").lower()
            for part in raw.split(",")
            if part.strip()
        )
