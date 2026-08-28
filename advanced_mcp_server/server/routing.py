# @gateway_route decorator for adv_gateway endpoints

from odoo import http

ADV_GATEWAY_MAX_BODY = 10 * 1024 * 1024  # 10 MiB


def gateway_route(routes=None, **kwargs):
    """Route decorator for Advanced MCP Server gateway endpoints."""
    kwargs.setdefault("type", "adv_gateway")
    kwargs.setdefault("auth", "adv_gateway")
    kwargs.setdefault("csrf", False)
    kwargs.setdefault("save_session", False)
    kwargs.setdefault("cors", "*")
    kwargs.setdefault("max_content_length", ADV_GATEWAY_MAX_BODY)
    if routes is None:
        return http.route(**kwargs)
    return http.route(routes, **kwargs)
