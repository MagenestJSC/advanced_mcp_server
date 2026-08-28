# Custom HTTP dispatcher for the adv_mcp routing type

import logging

import werkzeug.exceptions
from werkzeug.exceptions import HTTPException

from odoo import http
from odoo.http import CORS_MAX_AGE, Response

from . import protocol, helpers
from .sanitizer import GENERIC_ERROR_MESSAGE

_logger = logging.getLogger(__name__)

ADV_ROUTING_TYPE = "adv_gateway"


class AdvHttpGateway(http.Dispatcher):
    """Serve ``@gateway_route`` endpoints over HTTP POST."""

    routing_type = ADV_ROUTING_TYPE
    mimetypes = ("application/json",)

    def __init__(self, request):
        super().__init__(request)
        self.jsonrequest = {}
        self._ref = None

    @classmethod
    def is_compatible_with(cls, request):
        return True

    def pre_dispatch(self, rule, args):
        routing = rule.endpoint.routing
        self.request.session.can_save &= routing.get("save_session", True)

        max_content_length = routing.get("max_content_length")
        if max_content_length is not None:
            self.request.httprequest.max_content_length = max_content_length

        origin = self.request.httprequest.headers.get("Origin")
        allowed_origins = helpers.get_allowed_origins()
        if allowed_origins and origin:
            if origin.rstrip("/").lower() not in allowed_origins:
                _logger.info("adv_mcp request refused: Origin %r not in allowlist", origin)
                werkzeug.exceptions.abort(
                    self.request.make_json_response(
                        protocol.wrap_err(
                            protocol.ERR_FORBIDDEN, "Origin not allowed.", ref=self._ref
                        ),
                        status=403,
                    )
                )

        protocol_version = self.request.httprequest.headers.get("MCP-Protocol-Version")
        if protocol_version:
            from .handler import SUPPORTED_PROTOCOL_VERSIONS

            if protocol_version not in SUPPORTED_PROTOCOL_VERSIONS:
                _logger.info(
                    "adv_mcp request refused: unsupported MCP-Protocol-Version %r",
                    protocol_version,
                )
                werkzeug.exceptions.abort(
                    self.request.make_json_response(
                        protocol.wrap_err(
                            protocol.ERR_BAD_REQUEST,
                            "Unsupported MCP-Protocol-Version.",
                            ref=self._ref,
                        ),
                        status=400,
                    )
                )

        headers = self.request.future_response.headers
        if allowed_origins:
            if origin:
                headers.set("Access-Control-Allow-Origin", origin)
            headers.set("Vary", "Origin")
        else:
            headers.set("Access-Control-Allow-Origin", routing.get("cors") or "*")
        headers.set(
            "Access-Control-Allow-Methods",
            ", ".join(routing.get("methods") or ["POST"]),
        )

        if self.request.httprequest.method == "OPTIONS":
            headers.set("Access-Control-Max-Age", CORS_MAX_AGE)
            headers.set(
                "Access-Control-Allow-Headers",
                "Origin, X-Requested-With, Content-Type, Accept, Authorization, "
                "Range, MCP-Protocol-Version, Mcp-Session-Id",
            )
            werkzeug.exceptions.abort(Response(status=204))

    def dispatch(self, endpoint, args):
        try:
            self.jsonrequest = self.request.get_json_data()
        except (ValueError, AttributeError, RecursionError):
            self.request.params = dict(args)
            return self._error_response(protocol.ERR_BAD_REQUEST, "Parse error")

        self._ref = (
            self.jsonrequest.get("id") if isinstance(self.jsonrequest, dict) else None
        )
        try:
            self.request.params = {**self.jsonrequest, **args}
        except TypeError:
            self.request.params = dict(args)

        if self.request.db:
            result = self.request.registry["ir.http"]._dispatch(endpoint)
        else:
            result = endpoint(**self.request.params)
        if isinstance(result, Response):
            return result
        return self.request.make_json_response(result)

    def handle_error(self, exc):
        if isinstance(exc, HTTPException):
            return exc
        _logger.error("Unhandled error on adv_mcp route", exc_info=exc)
        return self._error_response(protocol.ERR_INTERNAL, GENERIC_ERROR_MESSAGE)

    def _error_response(self, error_type: str, detail: str):
        return self.request.make_json_response(
            protocol.wrap_err(error_type, detail, ref=self._ref)
        )
