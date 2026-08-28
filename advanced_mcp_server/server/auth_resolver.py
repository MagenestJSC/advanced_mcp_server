# Authentication utilities for adv_mcp

import functools
import logging

from odoo import SUPERUSER_ID
from odoo.http import request

from . import audit_writer
from .rate_limiter import RequestThrottle

_logger = logging.getLogger(__name__)

_AUTH_FAILURE_MAX = 20
_AUTH_FAILURE_WINDOW_SECONDS = 60
_auth_failure_limiter = RequestThrottle(_AUTH_FAILURE_WINDOW_SECONDS)


def _log_auth_failure(error_message, api_key_used=True):
    if _auth_failure_limiter.is_limited(
        (request.db, request.httprequest.remote_addr), _AUTH_FAILURE_MAX
    ):
        return
    audit_writer.push_event(
        SUPERUSER_ID,
        lambda ev: ev.record_event(
            "auth_failure",
            ip_address=request.httprequest.remote_addr,
            error_message=error_message,
            auth_method="api_key" if api_key_used else "bearer",
        ),
        "Failed to write auth-failure event",
    )


def get_user_from_api_key(api_key, *, allowed_scopes=("rpc",), log_failure=True):
    if not api_key:
        return None

    try:
        apikeys = request.env["res.users.apikeys"].sudo()
        user_id = None
        for scope in allowed_scopes:
            user_id = apikeys._check_credentials(scope=scope, key=api_key)
            if user_id:
                break
        if not user_id:
            if log_failure:
                _log_auth_failure("Invalid API key")
            return None
        users = request.env["res.users"].sudo()
        user = users.browse(user_id).exists()
        if user and user.active:
            return user
        else:
            if log_failure:
                _log_auth_failure("User not found or inactive")
            return None
    except Exception as e:
        _logger.exception("Error validating API key")
        if log_failure:
            _log_auth_failure(str(e))
        return None


def validate_api_key(req):
    api_key = req.httprequest.headers.get("X-API-Key")
    if not api_key:
        return None
    return get_user_from_api_key(api_key)


def get_user_from_session():
    try:
        user = request.env.user
        if user and user.id and user.id != request.env.ref("base.public_user").id:
            return user
    except Exception as e:
        _logger.debug("Session auth check failed: %s", e)
    return None


def require_auth(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        from . import helpers

        user = validate_api_key(request)
        if not user:
            user = get_user_from_session()

        if not user:
            if not request.httprequest.headers.get("X-API-Key"):
                _log_auth_failure("No valid API key or session", api_key_used=False)
            return helpers.error_response(
                "Authentication required. Provide a valid API key "
                "(X-API-Key header) or session cookie.",
                "E401",
                status=401,
            )

        kwargs["user"] = user
        return func(*args, **kwargs)

    return wrapper


require_api_key = require_auth
