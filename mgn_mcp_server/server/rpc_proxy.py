# XML-RPC proxy for the adv_mcp endpoint

import logging
import xmlrpc.client as xmlrpclib  # nosec
from datetime import datetime
from typing import Any, Optional, Tuple

import defusedxml.xmlrpc

from odoo import http
from odoo.http import request
from odoo.service import (
    common as common_service_root,
    db as db_service_root,
    model as model_service_root,
)

from odoo.addons.rpc.controllers.xmlrpc import dumps as odoo_dumps

from . import auth_resolver, helpers

_logger = logging.getLogger(__name__)
defusedxml.xmlrpc.monkey_patch()

XMLRPC_FAULT_CODES = {
    "bad_request": 400,
    "unauthorized": 401,
    "forbidden": 403,
    "not_found": 404,
    "rate_limit": 429,
    "internal_error": 500,
}


def _generate_xmlrpc_fault(code: int, message: str) -> str:
    fault = xmlrpclib.Fault(code, message)
    return xmlrpclib.dumps(fault, methodresponse=1, allow_none=1)


def _get_client_ip() -> Optional[str]:
    if request and hasattr(request, "httprequest"):
        return request.httprequest.remote_addr
    return None


def _dispatch_service_xmlrpc(controller_name: str, service_dispatch) -> Any:
    if not helpers.is_adv_server_enabled():
        fault_response = _generate_xmlrpc_fault(
            XMLRPC_FAULT_CODES["forbidden"],
            "Advanced MCP Server is disabled globally.",
        )
        return request.make_response(fault_response, [("Content-Type", "text/xml")])

    data = request.httprequest.data
    try:
        params, method = xmlrpclib.loads(data)
        result = service_dispatch(method, params)
        response_data = xmlrpclib.dumps((result,), methodresponse=1, allow_none=1)
        return request.make_response(response_data, [("Content-Type", "text/xml")])
    except xmlrpclib.Fault as e:
        _logger.warning(
            f"{controller_name} XML-RPC Fault: Code {e.faultCode}, String: {e.faultString}"
        )
        return request.make_response(
            xmlrpclib.dumps(e, methodresponse=1, allow_none=1),
            [("Content-Type", "text/xml")],
        )
    except Exception as e:
        error_msg = str(e)
        _logger.error("Error in %s: %s", controller_name, error_msg, exc_info=True)
        fault_response = _generate_xmlrpc_fault(
            XMLRPC_FAULT_CODES["internal_error"],
            f"{controller_name} Error: {error_msg}",
        )
        return request.make_response(fault_response, [("Content-Type", "text/xml")])


class AdvRpcCommonController(http.Controller):
    @http.route(
        "/mcp_server/xmlrpc/common",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def index(self, **kwargs):
        return _dispatch_service_xmlrpc("AdvRpcCommonController", common_service_root.dispatch)


class AdvRpcDatabaseController(http.Controller):
    @http.route(
        "/mcp_server/xmlrpc/db",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def index(self, **kwargs):
        return _dispatch_service_xmlrpc("AdvRpcDatabaseController", db_service_root.dispatch)


class AdvRpcController(http.Controller):
    def _validate_request(self, xmlrpc_method: str, params: list) -> None:
        if xmlrpc_method != "execute_kw":
            _logger.warning(
                f"AdvRpcController received non-execute_kw method: {xmlrpc_method}"
            )
            if request and hasattr(request, "env"):
                request.env["adv.event"].sudo().record_error(
                    error_message=f"AdvRpcController: Unsupported method {xmlrpc_method}. Only execute_kw is allowed.",
                    error_code="E400",
                    endpoint="/mcp_server/xmlrpc/object",
                    operation=xmlrpc_method,
                    ip_address=_get_client_ip(),
                )
            raise xmlrpclib.Fault(
                XMLRPC_FAULT_CODES["bad_request"],
                f"AdvRpcController: Unsupported method {xmlrpc_method}. Only execute_kw is allowed.",
            )

        if len(params) < 5:
            raise xmlrpclib.Fault(
                XMLRPC_FAULT_CODES["bad_request"],
                "AdvRpcController: Insufficient parameters for execute_kw.",
            )

    def _identify_user(self, auth_token: Any, uid: Any) -> Tuple[Optional[Any], Optional[int]]:
        user_obj = None
        user_id = None

        if isinstance(auth_token, str) and len(auth_token) > 20:
            user_obj = auth_resolver.get_user_from_api_key(auth_token)
            if user_obj:
                user_id = user_obj.id

        if not user_id and uid:
            user_id = uid

        return user_obj, user_id

    def _get_env_for_user(self, user_obj: Optional[Any], uid: Any) -> Any:
        if user_obj:
            return request.env(user=user_obj.id)

        if uid:
            try:
                return request.env(user=uid)
            except Exception as e:
                _logger.debug(f"Failed to create environment for uid {uid}: {e}")

        return request.env

    def _extract_record_ids(self, params: list) -> Optional[list]:
        if len(params) > 5 and isinstance(params[5], list):
            if params[5] and isinstance(params[5][0], int):
                return params[5]
        return None

    def _adv_object_dispatch(self, xmlrpc_method: str, params: list):
        self._validate_request(xmlrpc_method, params)

        uid = params[1]
        auth_token = params[2]
        model_method = helpers._one_line(params[4])

        try:
            model_name = helpers.sanitize_model_name(params[3])
        except ValueError as e:
            raise xmlrpclib.Fault(
                XMLRPC_FAULT_CODES["bad_request"], f"Invalid model name: {e}"
            ) from e

        user_obj, user_id = self._identify_user(auth_token, uid)
        env_for_check = self._get_env_for_user(user_obj, uid)

        start_time = datetime.now()
        ip_address = _get_client_ip()

        if not helpers.is_adv_server_enabled():
            env_for_check["adv.event"].sudo().record_access_denied(
                model_name=model_name,
                operation=model_method,
                user_id=user_id,
                endpoint="/mcp_server/xmlrpc/object",
                ip_address=ip_address,
                error_message="Advanced MCP Server is disabled.",
            )
            raise xmlrpclib.Fault(
                XMLRPC_FAULT_CODES["forbidden"],
                "Advanced MCP Server is disabled globally.",
            )

        if not helpers.is_model_accessible(env_for_check, model_name):
            env_for_check["adv.event"].sudo().record_access_denied(
                model_name=model_name,
                operation=model_method,
                user_id=user_id,
                endpoint="/mcp_server/xmlrpc/object",
                ip_address=ip_address,
                error_message=f"Access denied for model '{model_name}' method '{model_method}'.",
            )
            raise xmlrpclib.Fault(
                XMLRPC_FAULT_CODES["forbidden"],
                f"Access denied by adv_mcp for model '{model_name}' method '{model_method}'.",
            )

        op_map = helpers.XMLRPC_METHOD_OPERATION_MAP
        operation = op_map.get(model_method.lower())
        if not operation:
            raise xmlrpclib.Fault(
                XMLRPC_FAULT_CODES["forbidden"],
                f"adv_mcp: method '{model_method}' has no operation mapping.",
            )

        if not helpers.verify_model_operation(env_for_check, model_name, operation):
            raise xmlrpclib.Fault(
                XMLRPC_FAULT_CODES["forbidden"],
                f"adv_mcp: operation '{operation}' not allowed for '{model_name}'.",
            )

        try:
            result = model_service_root.dispatch(xmlrpc_method, params)

            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            env_for_check["adv.event"].sudo().record_access(
                model_name=model_name,
                operation=model_method,
                user_id=user_id,
                record_ids=self._extract_record_ids(params),
                endpoint="/mcp_server/xmlrpc/object",
                http_method="POST",
                duration_ms=duration_ms,
                ip_address=ip_address,
            )

            return result
        except Exception as e:
            duration_ms = int((datetime.now() - start_time).total_seconds() * 1000)
            env_for_check["adv.event"].sudo().record_error(
                error_message=str(e),
                error_code="E500",
                endpoint="/mcp_server/xmlrpc/object",
                model_name=model_name,
                operation=model_method,
                user_id=user_id,
                ip_address=ip_address,
            )
            raise

    @http.route(
        "/mcp_server/xmlrpc/object",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def index(self, **kwargs):
        if not helpers.is_adv_server_enabled():
            fault_response = _generate_xmlrpc_fault(
                XMLRPC_FAULT_CODES["forbidden"],
                "Advanced MCP Server is disabled globally.",
            )
            return request.make_response(fault_response, [("Content-Type", "text/xml")])

        data = request.httprequest.data
        try:
            params, method = xmlrpclib.loads(data)
            result = self._adv_object_dispatch(method, params)
            response_data = odoo_dumps((result,))
            return request.make_response(response_data, [("Content-Type", "text/xml")])
        except xmlrpclib.Fault as e:
            _logger.warning(
                f"AdvRpcController XML-RPC Fault: Code {e.faultCode}, String: {e.faultString}"
            )
            return request.make_response(
                xmlrpclib.dumps(e, methodresponse=1, allow_none=1),
                [("Content-Type", "text/xml")],
            )
        except Exception as e:
            error_msg = str(e)
            _logger.error(
                "Critical error in AdvRpcController dispatch: %s", error_msg, exc_info=True
            )
            fault_response = _generate_xmlrpc_fault(
                XMLRPC_FAULT_CODES["internal_error"],
                f"Internal Server Error in AdvRpcController: {error_msg}",
            )
            return request.make_response(fault_response, [("Content-Type", "text/xml")])
