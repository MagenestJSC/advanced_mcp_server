# Short-lived OAuth 2.1 authorization codes for the Adv MCP endpoint.

from datetime import timedelta

from authlib.oauth2.rfc6749 import AuthorizationCodeMixin

from odoo import api, fields, models
from odoo.http import request

from .hash_utils import sha256_hex as _sha256

_CODE_TTL_SECONDS = 60


class AdvOauthCode(models.Model, AuthorizationCodeMixin):
    _name = "adv.oauth.code"
    _description = "Adv MCP OAuth Authorization Code"
    _rec_name = "client"
    _order = "create_date desc"

    code_hash = fields.Char(
        string="Code Hash",
        required=True,
        index=True,
        copy=False,
        help="SHA-256 hash of the authorization code; the raw code is never stored.",
    )
    client = fields.Many2one(
        "adv.oauth.client",
        string="Client",
        required=True,
        index=True,
        ondelete="cascade",
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        required=True,
        index=True,
        ondelete="cascade",
    )
    redirect_uri = fields.Char(string="Redirect URI")
    scope = fields.Char(string="Scope")
    code_challenge = fields.Char(string="Code Challenge")
    code_challenge_method = fields.Char(string="Code Challenge Method", default="S256")
    resource = fields.Char(string="Resource")
    expires_at = fields.Datetime(string="Expires At", required=True, index=True)
    used = fields.Boolean(default=False, copy=False)
    refresh_family_id = fields.Char(string="Refresh Family", copy=False)

    @api.model
    def _save_code(self, code, oauth2_request):
        # Persist a freshly issued authorization code (stored hashed).
        data = oauth2_request.payload.data
        _saved_scope = getattr(request, "_adv_granted_scope", None)
        vals = {
            "code_hash": _sha256(code),
            "client": oauth2_request.client.id,
            "user_id": oauth2_request.user.id,
            "redirect_uri": oauth2_request.payload.redirect_uri,
            "scope": _saved_scope or oauth2_request.scope or "",
            "code_challenge": data.get("code_challenge"),
            "code_challenge_method": data.get("code_challenge_method") or "S256",
            "resource": data.get("resource"),
            "expires_at": fields.Datetime.now() + timedelta(seconds=_CODE_TTL_SECONDS),
        }
        return self.create(vals)

    @api.model
    def _get_valid_code(self, code, client):
        # Return the unredeemed, unexpired code for client (or empty recordset).
        self.env.cr.execute(
            """
            SELECT id FROM adv_oauth_code
            WHERE code_hash = %s AND client = %s
              AND used = FALSE AND expires_at > %s
            LIMIT 1
            FOR UPDATE
            """,
            (_sha256(code), client.id, fields.Datetime.now()),
        )
        row = self.env.cr.fetchone()
        return self.browse(row[0]) if row else self.browse()

    def _consume(self):
        # Mark the code redeemed so replays are detectable.
        self.write({"used": True})

    @api.model
    def _detect_reuse(self, code):
        # Revoke a token family when its already-redeemed code is replayed.
        spent = self.search(
            [("code_hash", "=", _sha256(code)), ("used", "=", True)], limit=1
        )
        if spent and spent.refresh_family_id:
            self.env["adv.oauth.token"]._revoke_family(spent.refresh_family_id)

    def get_redirect_uri(self):
        return self.redirect_uri

    def get_scope(self):
        return self.scope or ""

    def get_auth_time(self):
        return None

    def get_nonce(self):
        return None
