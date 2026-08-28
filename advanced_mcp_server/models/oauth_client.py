# OAuth 2.1 client registry for the Adv MCP endpoint.

from authlib.oauth2.rfc6749 import ClientMixin

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from ..server.oauth.grants import (
    SUPPORTED_GRANT_TYPES,
    SUPPORTED_RESPONSE_TYPES,
    SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS,
    expand_scopes as expand_registered_scopes,
)

_DEFAULT_GRANT_TYPES = " ".join(SUPPORTED_GRANT_TYPES)
_DEFAULT_RESPONSE_TYPES = " ".join(SUPPORTED_RESPONSE_TYPES)
_DEFAULT_TOKEN_ENDPOINT_AUTH_METHOD = " ".join(SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS)


class AdvOauthClient(models.Model, ClientMixin):
    _name = "adv.oauth.client"
    _description = "Adv MCP OAuth Client"
    _rec_name = "client_id"
    _order = "create_date desc"

    client_id = fields.Char(
        string="Client ID",
        required=True,
        copy=False,
        help="Public identifier issued to the client at registration.",
    )
    client_name = fields.Char(
        string="Name",
        help="Human-readable name the client supplied at registration.",
    )
    redirect_uris = fields.Text(
        string="Redirect URIs",
        help="Whitespace-separated allow-list of redirect URIs.",
    )
    grant_types = fields.Char(
        string="Grant Types",
        default=_DEFAULT_GRANT_TYPES,
    )
    response_types = fields.Char(
        string="Response Types",
        default=_DEFAULT_RESPONSE_TYPES,
    )
    token_endpoint_auth_method = fields.Char(
        string="Token Endpoint Auth Method",
        default=_DEFAULT_TOKEN_ENDPOINT_AUTH_METHOD,
    )
    scope = fields.Char(string="Scope")
    created_via = fields.Char(string="Created Via", index=True)
    active = fields.Boolean(default=True)
    create_date = fields.Datetime(index=True)

    _unique_client_id = models.Constraint(
        "UNIQUE(client_id)",
        "OAuth client_id must be unique.",
    )

    def action_deactivate(self):
        # Admin action: block this client's access.
        if not self.env.user.has_group("advanced_mcp_server.group_adv_admin"):
            raise AccessError(
                _("Only Adv MCP administrators may change OAuth client status.")
            )
        self.sudo().write({"active": False})

    def action_activate(self):
        # Admin action: re-enable a deactivated client.
        if not self.env.user.has_group("advanced_mcp_server.group_adv_admin"):
            raise AccessError(
                _("Only Adv MCP administrators may change OAuth client status.")
            )
        self.sudo().write({"active": True})

    @api.depends("client_name", "client_id")
    def _compute_display_name(self):
        for client in self:
            client.display_name = client.client_name or client.client_id

    def _redirect_uri_list(self):
        self.ensure_one()
        return (self.redirect_uris or "").split()

    @api.model
    def _register_client(self, client_info, client_metadata):
        redirect_uris = client_metadata.get("redirect_uris") or []
        vals = {
            "client_id": client_info["client_id"],
            "client_name": client_metadata.get("client_name") or "",
            "redirect_uris": "\n".join(redirect_uris),
            "scope": client_metadata.get("scope") or "",
            "grant_types": _DEFAULT_GRANT_TYPES,
            "response_types": _DEFAULT_RESPONSE_TYPES,
            "token_endpoint_auth_method": _DEFAULT_TOKEN_ENDPOINT_AUTH_METHOD,
            "created_via": "dcr",
        }
        return self.create(vals)

    def get_client_id(self):
        return self.client_id

    def get_default_redirect_uri(self):
        uris = self._redirect_uri_list()
        return uris[0] if uris else None

    def get_allowed_scope(self, scope):
        if not scope:
            return self.scope or ""
        if not self.scope:
            return scope
        allowed = expand_registered_scopes(self.scope)
        filtered = " ".join(s for s in scope.split() if s in allowed)
        return filtered or self.scope

    def check_redirect_uri(self, redirect_uri):
        return redirect_uri in self._redirect_uri_list()

    def check_client_secret(self, client_secret):
        return False

    def check_endpoint_auth_method(self, method, endpoint):
        if endpoint == "token":
            return method == self.token_endpoint_auth_method
        return True

    def check_response_type(self, response_type):
        return response_type in (self.response_types or "").split()

    def check_grant_type(self, grant_type):
        return grant_type in (self.grant_types or "").split()
