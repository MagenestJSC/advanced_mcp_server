# Main REST gateway for adv_mcp

import functools
import logging
from datetime import datetime

from odoo import http
from odoo.http import request

from . import auth_resolver, helpers

_logger = logging.getLogger(__name__)


def require_adv_enabled(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if not helpers.is_adv_server_enabled():
            return helpers.error_response(
                message="Advanced MCP Server is disabled globally.",
                code="E503",
                status=503,
            )
        return func(*args, **kwargs)

    return wrapper


class AdvGatewayController(http.Controller):

    @http.route(
        "/mcp_server/health", type="http", auth="none", methods=["GET"], csrf=False
    )
    @require_adv_enabled
    def health_check(self, **kwargs):
        data = {"status": "ok", "adv_server_version": helpers.server_version()}
        return helpers.success_response(data)

    @http.route(
        "/mcp_server/system/info", type="http", auth="public", methods=["GET"], csrf=False
    )
    @auth_resolver.require_api_key
    @require_adv_enabled
    def system_info(self, **kwargs):
        user = kwargs.get("user")
        env = request.env(user=user.id) if user else request.env
        return helpers.success_response(helpers.system_info(env))

    @http.route(
        "/mcp_server/auth/validate",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    @auth_resolver.require_api_key
    @require_adv_enabled
    def validate_auth(self, **kwargs):
        user = kwargs.get("user")
        if user:
            api_key = request.httprequest.headers.get("X-API-Key")
            auth_method = "api_key" if api_key else "session"
            return helpers.success_response(
                {"valid": True, "user_id": user.id, "auth_method": auth_method}
            )
        else:
            return helpers.error_response(
                "API key validation failed unexpectedly.", "E500", status=500
            )

    @http.route(
        "/mcp_server/models", type="http", auth="public", methods=["GET"], csrf=False
    )
    @auth_resolver.require_api_key
    @require_adv_enabled
    def get_models(self, **kwargs):
        user = kwargs.get("user")
        env = request.env(user=user.id) if user else request.env
        return helpers.success_response({"models": helpers.list_accessible_models(env)})

    @http.route(
        "/mcp_server/models/<string:model>/access",
        type="http",
        auth="public",
        methods=["GET"],
        csrf=False,
    )
    @auth_resolver.require_api_key
    @require_adv_enabled
    def get_model_access(self, model, **kwargs):
        start_time = datetime.now()
        user = kwargs.get("user")
        env = request.env(user=user.id) if user else request.env

        try:
            model_tech_name = helpers.sanitize_model_name(model)
        except ValueError as e:
            return helpers.error_response(message=str(e), code="E400", status=400)

        ir_model = (
            env["ir.model"].sudo().search([("model", "=", model_tech_name)], limit=1)
        )
        if not ir_model:
            error_message = f"Model '{model_tech_name}' not found in Odoo instance."
            env["adv.event"].sudo().record_error(
                error_message=error_message,
                error_code="E404",
                endpoint=request.httprequest.path,
                model_name=model_tech_name,
                operation="access",
                user_id=user.id if user else None,
                ip_address=request.httprequest.remote_addr,
            )
            return helpers.error_response(message=error_message, code="E404", status=404)

        is_enabled = helpers.is_model_accessible(env, model_tech_name)

        if not is_enabled:
            error_message = f"Model '{model_tech_name}' is not enabled for adv_mcp access."
            env["adv.event"].sudo().record_access_denied(
                model_name=model_tech_name,
                operation="access",
                user_id=user.id if user else None,
                endpoint=request.httprequest.path,
                ip_address=request.httprequest.remote_addr,
                error_message=error_message,
            )
            return helpers.error_response(message=error_message, code="E403", status=403)

        access_rec = env["adv.module.access"].sudo()
        operations = {
            op: access_rec.check_model_operation_enabled(model_tech_name, op)
            for op in ("read", "create", "write", "unlink")
        }

        duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
        env["adv.event"].sudo().record_access(
            model_name=model_tech_name,
            operation="access",
            user_id=user.id if user else None,
            endpoint=request.httprequest.path,
            http_method=request.httprequest.method,
            duration_ms=duration_ms,
            ip_address=request.httprequest.remote_addr,
        )

        return helpers.success_response(
            {"model": model_tech_name, "enabled": is_enabled, "operations": operations}
        )
