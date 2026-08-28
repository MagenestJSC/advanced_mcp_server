# OAuth 2.1 flow controllers for adv_mcp

import logging
from urllib.parse import quote, urlsplit

from authlib.common.urls import add_params_to_uri
from authlib.oauth2 import OAuth2Error
from psycopg2 import OperationalError
from werkzeug.exceptions import BadRequest, UnsupportedMediaType

from odoo import _, http
from odoo.http import request

from ..rate_limiter import RequestThrottle
from .grants import (
    ADV_SCOPES,
    _is_allowed_redirect_uri,
    client_write_allowed,
    get_auth_server,
    read_only_scope,
)
from .discovery import oauth_guard, base_url

_logger = logging.getLogger(__name__)

OAUTH_MAX_CONTENT_LENGTH = 256 * 1024  # 256 KiB

_DCR_MAX_REGISTRATIONS = 20
_DCR_WINDOW_SECONDS = 3600
_dcr_limiter = RequestThrottle(_DCR_WINDOW_SECONDS)

_TOKEN_MAX_REQUESTS = 60
_TOKEN_WINDOW_SECONDS = 60
_token_limiter = RequestThrottle(_TOKEN_WINDOW_SECONDS)

_AUTHORIZE_PARAMS = (
    "response_type",
    "client_id",
    "redirect_uri",
    "scope",
    "state",
    "code_challenge",
    "code_challenge_method",
    "resource",
)


def _dcr_rate_limited(ip):
    return _dcr_limiter.is_limited((request.db, ip), _DCR_MAX_REGISTRATIONS)


def _token_rate_limited(ip):
    return _token_limiter.is_limited((request.db, ip), _TOKEN_MAX_REQUESTS)


def _oauth_error_response(error, status, description=None):
    body = {"error": error}
    if description is not None:
        body["error_description"] = description
    return request.make_json_response(body, status=status)


def _add_issuer(response):
    location = response.headers.get("Location")
    if location:
        response.headers["Location"] = add_params_to_uri(location, [("iss", base_url())])
    return response


def _security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["Cache-Control"] = "no-store"
    return response


def _render_authorize_error(description):
    response = request.render(
        "mgn_mcp_server.adv_oauth_error", {"error_description": description}
    )
    response.status_code = 400
    return _security_headers(response)


def _pkce_s256_present():
    values = request.httprequest.values
    challenge = values.get("code_challenge")
    method = values.get("code_challenge_method", "S256")
    return bool(challenge) and method == "S256"


def _resource_accepted(resource):
    from .discovery import accepted_resources
    if not resource:
        return True
    accepted = {url.rstrip("/") for url in accepted_resources()}
    return resource.rstrip("/") in accepted


def _render_consent(grant):
    client = grant.client
    redirect_uri = grant.redirect_uri or ""
    parts = urlsplit(redirect_uri)
    scheme, hostname, port = parts.scheme, parts.hostname, parts.port
    if scheme and hostname:
        host = f"{hostname}:{port}" if port else hostname
        redirect_origin = f"{scheme}://{host}"
    else:
        redirect_origin = redirect_uri
    values = request.httprequest.values
    qcontext = {
        "user_name": request.env.user.name,
        "client_label": client.client_name or client.client_id,
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "redirect_origin": redirect_origin,
        "show_write_checkbox": (
            not read_only_scope(grant.request.payload.scope)
            and client_write_allowed(client)
        ),
        "switch_user_url": "/web/session/logout?redirect="
        + quote(request.httprequest.full_path, safe=""),
        "oauth_params": {
            name: values.get(name) for name in _AUTHORIZE_PARAMS if values.get(name)
        },
        "csrf_token": request.csrf_token(),
    }
    response = request.render("mgn_mcp_server.adv_oauth_consent", qcontext)
    return _security_headers(response)


class AdvOAuthController(http.Controller):

    @http.route(
        "/mcp_server/oauth/authorize",
        type="http",
        auth="user",
        methods=["GET", "POST"],
        csrf=True,
        sitemap=False,
        max_content_length=OAUTH_MAX_CONTENT_LENGTH,
    )
    @oauth_guard
    def authorize(self, **kwargs):
        server = get_auth_server()
        try:
            grant = server.get_consent_grant(end_user=request.env.user)
        except OAuth2Error as error:
            return _render_authorize_error(error.description or error.error)

        if not _is_allowed_redirect_uri(grant.redirect_uri or ""):
            return _render_authorize_error(
                _("The redirect URI registered for this client is not allowed.")
            )

        if not _pkce_s256_present():
            return _render_authorize_error(
                _(
                    "Proof Key for Code Exchange (PKCE) with the S256 method is "
                    "required to authorize this request."
                )
            )

        if not _resource_accepted(request.httprequest.values.get("resource")):
            return _render_authorize_error(
                _("The requested resource is not served by this server.")
            )

        # Auto-approve on GET: skip the consent screen once the user is authenticated.
        # On POST we still honour an explicit deny so existing integrations keep working.
        if request.httprequest.method == "GET":
            granted = (
                "adv:write"
                if (
                    not read_only_scope(grant.request.payload.scope)
                    and client_write_allowed(grant.client)
                )
                else "adv:read"
            )
            request._adv_granted_scope = granted
            response = server.create_authorization_response(
                grant=grant, grant_user=request.env.user
            )
            return _add_issuer(response)

        approved = request.params.get("decision") == "allow"
        grant_user = request.env.user if approved else None
        if approved:
            granted = (
                "adv:write"
                if (
                    not read_only_scope(grant.request.payload.scope)
                    and request.params.get("grant_write")
                )
                else "adv:read"
            )
            if granted == "adv:write" and not client_write_allowed(grant.client):
                granted = "adv:read"
            request._adv_granted_scope = granted
        response = server.create_authorization_response(grant=grant, grant_user=grant_user)
        return _add_issuer(response)

    @http.route(
        "/mcp_server/oauth/token",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        readonly=False,
        sitemap=False,
        max_content_length=OAUTH_MAX_CONTENT_LENGTH,
    )
    @oauth_guard
    def token(self, **kwargs):
        if _token_rate_limited(request.httprequest.remote_addr):
            return _oauth_error_response(
                "temporarily_unavailable", 429, "Too many token requests."
            )
        try:
            return get_auth_server().create_token_response()
        except OperationalError:
            raise
        except Exception:
            _logger.exception("OAuth token endpoint failed")
            return _oauth_error_response("server_error", 500)

    @http.route(
        "/mcp_server/oauth/register",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
        readonly=False,
        sitemap=False,
        max_content_length=OAUTH_MAX_CONTENT_LENGTH,
    )
    @oauth_guard
    def register(self, **kwargs):
        if _dcr_rate_limited(request.httprequest.remote_addr):
            return _oauth_error_response(
                "temporarily_unavailable", 429, "Too many registration requests."
            )
        try:
            return get_auth_server().create_endpoint_response("client_registration")
        except (BadRequest, UnsupportedMediaType):
            return _oauth_error_response(
                "invalid_client_metadata", 400, "Invalid request body."
            )
        except ValueError:
            _logger.warning("OAuth DCR rejected a redirect_uri Authlib could not parse")
            return _oauth_error_response(
                "invalid_client_metadata", 400, "Invalid redirect_uri."
            )
        except OperationalError:
            raise
        except Exception:
            _logger.exception("OAuth register endpoint failed")
            return _oauth_error_response("server_error", 500)
