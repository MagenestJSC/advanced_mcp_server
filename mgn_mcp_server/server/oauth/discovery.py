# Well-known metadata routes for adv_mcp OAuth server

from functools import wraps

from odoo import http
from odoo.http import Response, request

from .. import helpers
from .grants import ADV_SCOPES, SUPPORTED_GRANT_TYPES, SUPPORTED_RESPONSE_TYPES, SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS


def base_url():
    return request.httprequest.url_root.rstrip("/")


def resource_url():
    return base_url() + "/mcp_server"


def accepted_resources():
    base = base_url()
    return (base + "/mcp_server", base + "/mcp_server/rpc")


def metadata_url():
    return base_url() + "/.well-known/oauth-protected-resource"


def oauth_guard(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not (helpers.is_adv_server_enabled() and helpers.is_adv_oauth_enabled(request.env)):
            return Response(status=404)
        return fn(*args, **kwargs)

    return wrapper


class AdvDiscoveryController(http.Controller):

    @http.route(
        [
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp_server",
        ],
        type="http",
        auth="none",
        methods=["GET"],
        sitemap=False,
    )
    @oauth_guard
    def protected_resource_metadata(self, **kwargs):
        metadata = {
            "resource": resource_url(),
            "authorization_servers": [base_url()],
            "bearer_methods_supported": ["header"],
            "scopes_supported": ADV_SCOPES,
        }
        return request.make_json_response(metadata)

    @http.route(
        "/.well-known/oauth-authorization-server",
        type="http",
        auth="none",
        methods=["GET"],
        sitemap=False,
    )
    @oauth_guard
    def authorization_server_metadata(self, **kwargs):
        base = base_url()
        metadata = {
            "issuer": base,
            "authorization_endpoint": base + "/mcp_server/oauth/authorize",
            "token_endpoint": base + "/mcp_server/oauth/token",
            "registration_endpoint": base + "/mcp_server/oauth/register",
            "response_types_supported": SUPPORTED_RESPONSE_TYPES,
            "grant_types_supported": SUPPORTED_GRANT_TYPES,
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS,
            "scopes_supported": ADV_SCOPES,
        }
        return request.make_json_response(metadata)
