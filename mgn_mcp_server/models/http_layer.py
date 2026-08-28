# HTTP auth layer for the adv_mcp endpoint.
# Provides auth='adv_mcp' method: accepts Bearer tokens (OAuth or API key)
# and wraps internal errors on adv_mcp routes as JSON-RPC error responses.

import re

from werkzeug.datastructures import WWWAuthenticate
from werkzeug.exceptions import HTTPException, Unauthorized

from odoo import SUPERUSER_ID, models
from odoo.http import request

from ..server.auth_resolver import _log_auth_failure, get_user_from_api_key
from ..server.protocol import ERR_INTERNAL, wrap_err
from ..server.oauth.discovery import (
    accepted_resources as accepted_resource_urls,
    metadata_url as protected_resource_metadata_url,
)
from ..server.helpers import is_adv_oauth_enabled as is_oauth_enabled
from ..server.sanitizer import GENERIC_ERROR_MESSAGE
from ..server.rate_limiter import RequestThrottle as SlidingWindowLimiter

ADV_ROUTING_TYPE = "adv_gateway"

_BEARER_FAILURE_MAX = 20
_BEARER_FAILURE_WINDOW_SECONDS = 60
_bearer_failure_limiter = SlidingWindowLimiter(_BEARER_FAILURE_WINDOW_SECONDS)

_AUTH_SCHEME_NAMES = {"bearer", "basic", "digest", "negotiate", "ntlm"}


class AdvHttpLayer(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _auth_method_adv_gateway(cls):
        # Authenticate an /mcp_server Bearer token (OAuth or API key).
        header = request.httprequest.headers.get("Authorization")
        token = cls._extract_bearer_token(header)
        if not token:
            if header:
                _log_auth_failure("Malformed Authorization header", api_key_used=False)
            raise cls._unauthorized_response(
                "Missing credentials; provide an "
                "'Authorization: Bearer <token>' header."
            )

        user = get_user_from_api_key(
            token, allowed_scopes=("adv", "rpc"), log_failure=False
        )
        if user:
            request._adv_auth_method = "api_key"
            cls._bind_user(user.id)
            return

        oauth_user_id = cls._resolve_token(token)
        if oauth_user_id:
            cls._bind_user(oauth_user_id)
            return

        if not _bearer_failure_limiter.is_limited(
            (request.db, request.httprequest.remote_addr), _BEARER_FAILURE_MAX
        ):
            _log_auth_failure("Invalid or expired credentials", api_key_used=False)
        raise cls._unauthorized_response("Invalid or expired credentials.")

    @staticmethod
    def _extract_bearer_token(header):
        if not header:
            return None
        match = re.match(r"^bearer\s+(.+)$", header, re.IGNORECASE)
        if match:
            return match.group(1)
        bare = header.strip()
        if (
            bare
            and not re.search(r"\s", bare)
            and bare.lower() not in _AUTH_SCHEME_NAMES
        ):
            return bare
        return None

    @classmethod
    def _bind_user(cls, uid):
        # Bind the current request to uid without persisting a session.
        # su=False is explicit: pre-auth env may carry su=True (custom routing type),
        # which would bypass all ORM access checks for non-superusers.
        request.update_env(user=uid, su=(uid == SUPERUSER_ID))
        request.update_context(**request.env["res.users"].context_get())
        request.session.can_save = False

    @classmethod
    def _resolve_token(cls, raw_token):
        # Validate a Bearer as an OAuth access token and check audience.
        if not is_oauth_enabled(request.env):
            return None
        token = (
            request.env["adv.oauth.token"]
            .sudo()
            ._get_valid_access_token(raw_token)
        )
        if not token:
            return None
        if not cls._audience_ok(token.audience):
            return None
        if not token.user_id.active:
            return None
        if not token.client.active:
            return None
        request._adv_oauth_scope = token.scope or ""
        request._adv_auth_method = "oauth"
        request._adv_oauth_client_id = token.client.id
        return token.user_id.id

    @staticmethod
    def _audience_ok(audience):
        if not audience:
            return False
        return audience.rstrip("/") in {
            url.rstrip("/") for url in accepted_resource_urls()
        }

    @staticmethod
    def _unauthorized_response(description):
        # Build a 401 with RFC 9728 protected-resource pointer.
        if is_oauth_enabled(request.env):
            challenge = WWWAuthenticate(
                "Bearer",
                {"resource_metadata": protected_resource_metadata_url()},
            )
        else:
            challenge = WWWAuthenticate("Bearer")
        return Unauthorized(description, www_authenticate=challenge)

    @classmethod
    def _handle_error(cls, exception):
        if cls._is_adv_mcp_request() and not isinstance(exception, HTTPException):
            ref = getattr(request.dispatcher, "_ref", None)
            return request.make_json_response(
                wrap_err(ERR_INTERNAL, GENERIC_ERROR_MESSAGE, ref=ref)
            )
        return super()._handle_error(exception)

    @classmethod
    def _is_adv_mcp_request(cls):
        dispatcher = getattr(request, "dispatcher", None)
        return getattr(dispatcher, "routing_type", None) == ADV_ROUTING_TYPE
