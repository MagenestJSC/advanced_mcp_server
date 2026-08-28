from odoo import fields, models


class IrModuleModule(models.Model):
    """Expose icon_image as avatar_128 so many2one_avatar widget renders icons."""

    _inherit = "ir.module.module"

    avatar_128 = fields.Binary(related="icon_image", store=False)
