# Native adv_mcp gateway endpoint (POST /mcp_server)

import json
import logging
from datetime import datetime

from psycopg2 import OperationalError

from odoo import _, http
from odoo.exceptions import AccessError
from odoo.http import Response, request

from . import audit_writer as _aw, sanitizer, protocol, rate_limiter, helpers
from .oauth.grants import grants_write
from .routing import gateway_route

_logger = logging.getLogger(__name__)

_rate_limit_audit_limiter = rate_limiter.RequestThrottle(
    rate_limiter.THROTTLE_WINDOW_MINUTES * 60
)

_AUDIT_WRITE_MAX = 20
_AUDIT_WRITE_WINDOW_SECONDS = 60
_audit_write_limiter = rate_limiter.RequestThrottle(_AUDIT_WRITE_WINDOW_SECONDS)

# Route dispatch table: mcp_method → handler method name on AdvMCPHandler
_ROUTE_TABLE: dict[str, str] = {}


def _handles(mcp_method: str):
    """Register a handler method for an MCP method name."""
    def decorator(fn):
        _ROUTE_TABLE[mcp_method] = fn.__name__
        return fn
    return decorator


def _capability_error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": True}


class _CapabilityResultError(Exception):
    pass


PREFERRED_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18")

SERVER_NAME = "advanced-mcp-server"
SERVER_DESCRIPTION = (
    "Advanced Odoo MCP server: read and write Odoo records with per-model access "
    "control, audit logging and rate limiting."
)


class AdvMCPHandler(http.Controller):
    """Gateway handler backing the adv_mcp /mcp_server endpoint."""

    _FLOW_CONTROL_EXEMPT = frozenset(
        {
            "notifications/initialized",
            "notifications/cancelled",
            "notifications/progress",
            "notifications/roots/list_changed",
        }
    )

    @gateway_route(["/mcp_server", "/mcp_server/rpc"], methods=["POST"])
    def process(self, **kwargs):
        ref = kwargs.get("id")

        if not helpers.is_adv_server_enabled():
            return protocol.wrap_err(
                protocol.ERR_SERVER,
                _("Advanced MCP server is disabled globally."),
                ref=ref,
            )

        try:
            method, args, ref = protocol.parse_envelope(kwargs)
        except protocol.GatewayError as exc:
            return protocol.wrap_err(exc.error_type, exc.detail, ref=ref)

        throttled = self._apply_flow_control(method, ref)
        if throttled is not None:
            return throttled

        fn_name = _ROUTE_TABLE.get(method)
        try:
            if fn_name is not None:
                result = getattr(self, fn_name)(args)
            elif method.startswith("notifications/"):
                result = self._notification_ack(args)
            else:
                return protocol.wrap_err(
                    protocol.ERR_NOT_FOUND,
                    _("Unknown capability: '%(method)s' is not registered", method=method),
                    ref=ref,
                )
        except protocol.GatewayError as exc:
            return protocol.wrap_err(exc.error_type, exc.detail, ref=ref, hint=exc.data)

        if isinstance(result, Response):
            return result
        return protocol.wrap_ok(result, ref=ref)

    def _apply_flow_control(self, method: str, ref):
        if not self._subject_to_flow_control(method):
            return None
        if not rate_limiter.throttling_active():
            return None

        uid = request.env.uid
        dbname = request.env.cr.dbname
        if not rate_limiter.get_throttle().is_limited(
            (dbname, uid), rate_limiter.max_requests_for_uid(uid)
        ):
            return None

        if not _rate_limit_audit_limiter.is_limited((dbname, uid), 1):
            self._record_flow_limit_exceeded(uid)

        retry_after = rate_limiter.THROTTLE_WINDOW_MINUTES * 60
        return request.make_json_response(
            protocol.wrap_err(
                protocol.ERR_SERVER,
                _("Request quota reached — too many requests."),
                ref=ref,
            ),
            headers=[("Retry-After", str(retry_after))],
            status=429,
        )

    def _subject_to_flow_control(self, method: str) -> bool:
        return method != "ping" and method not in self._FLOW_CONTROL_EXEMPT

    def _emit_audit(self, uid, write_log, failure_message, *args):
        if _audit_write_limiter.is_limited(
            (request.db, request.httprequest.remote_addr), _AUDIT_WRITE_MAX
        ):
            return
        _aw.push_event(uid, write_log, failure_message, *args)

    def _record_flow_limit_exceeded(self, uid):
        ip = request.httprequest.remote_addr
        self._emit_audit(
            uid,
            lambda adv_log: adv_log.record_quota_exceeded(
                user_id=uid,
                endpoint="/mcp_server",
                ip_address=ip,
            ),
            "adv_mcp rate-limit audit logging failed",
        )

    def _write_permitted(self) -> bool:
        scope = getattr(request, "_adv_oauth_scope", None)
        return not scope or grants_write(scope)

    def _requires_write_scope(self, annotations: dict) -> bool:
        return annotations.get("readOnlyHint") is not True

    @_handles("initialize")
    def _initialize(self, args: dict) -> dict:
        requested = args.get("protocolVersion")
        proto_version = (
            requested if requested in SUPPORTED_PROTOCOL_VERSIONS
            else PREFERRED_PROTOCOL_VERSION
        )
        payload = {
            "protocolVersion": proto_version,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {
                "name": SERVER_NAME,
                "version": helpers.server_version(),
                "description": SERVER_DESCRIPTION,
            },
        }
        try:
            payload["instructions"] = helpers.describe_session(request.env)
        except Exception:
            _logger.exception("Failed to build adv_mcp initialize instructions")
        return payload

    @_handles("ping")
    def _ping(self, args: dict) -> dict:
        return {}

    @_handles("tools/list")
    def _enumerate_tools(self, args: dict) -> dict:
        host = request.env["adv.tool.mixin"]
        index = host._get_adv_tools()
        write_ok = self._write_permitted()

        built_in = [
            {
                "name": name,
                "description": meta["description"],
                "inputSchema": host._adv_live_input_schema(meta["input_schema"]),
                **({"title": meta["title"]} if meta.get("title") else {}),
                **({"annotations": meta["annotations"]} if meta.get("annotations") else {}),
            }
            for name, meta in index.items()
            if write_ok or not self._requires_write_scope(meta["annotations"])
        ]

        visible = request.env["adv.custom.tool"]._visible_tools()
        custom = []
        for ct in visible.sudo():
            annotations = {
                "readOnlyHint": ct.is_readonly,
                "destructiveHint": not ct.is_readonly,
            }
            if self._requires_write_scope(annotations) and not write_ok:
                continue
            schema = ct._parsed_input_schema()
            if schema is None:
                _logger.warning(
                    "Skipping custom tool '%s' in tools/list: malformed input_schema.",
                    ct.name,
                )
                continue
            custom.append({
                "name": ct.name,
                "title": ct.name,
                "description": ct.description,
                "inputSchema": schema,
                "annotations": annotations,
            })

        return {"tools": built_in + custom}

    @staticmethod
    def _missing_args(schema: dict, incoming: dict) -> list[str]:
        required = schema.get("required", [])
        if not isinstance(required, list):
            required = []
        return [k for k in required if isinstance(k, str) and k not in incoming]

    @_handles("tools/call")
    def _invoke_tool(self, args: dict) -> dict:
        name = args.get("name")
        if not isinstance(name, str) or not name:
            self._reject_call(name, _("Invalid params: 'name' must be a non-empty string."))

        index = request.env["adv.tool.mixin"]._get_adv_tools()
        meta = index.get(name)
        custom_tool = None
        if meta is None:
            meta, custom_tool = self._resolve_custom_tool(name)

        incoming = args.get("arguments") or {}
        if not isinstance(incoming, dict):
            return self._bad_input(
                name, _("Invalid arguments: 'arguments' must be an object.")
            )

        model_name = incoming.get("model") if isinstance(incoming.get("model"), str) else None
        operation = meta.get("operation") or name
        if name == "call_model_method":
            raw_method = incoming.get("method")
            if isinstance(raw_method, str) and raw_method.strip():
                operation = raw_method.strip()

        record_ids = self._extract_ids(incoming)
        req_summary = ", ".join(sorted(incoming)) if incoming else None
        resp_summary = None
        ts = datetime.now()
        error_detail = None
        rejected = False
        concurrency_retry = False

        bound = (
            None
            if custom_tool is not None
            else getattr(request.env["adv.tool.mixin"], meta["method_name"])
        )
        try:
            if custom_tool is not None and not custom_tool._user_can_run():
                rejected = True
                return self._deny(
                    name, operation, model_name,
                    _("You are not allowed to run the '%(name)s' tool.", name=name),
                )

            if self._requires_write_scope(meta["annotations"]) and not self._write_permitted():
                rejected = True
                return self._deny(
                    name, operation, model_name,
                    _(
                        "This connection was authorized read-only (scope "
                        "adv:read); the '%(name)s' tool requires write access.",
                        name=name,
                    ),
                )

            missing = self._missing_args(meta["input_schema"], incoming)
            if missing:
                rejected = True
                return self._bad_input(
                    name,
                    _("Missing required argument(s): %(fields)s", fields=", ".join(missing)),
                )

            try:
                with request.env.cr.savepoint():
                    if custom_tool is not None:
                        raw_out = custom_tool._run_tool(incoming)
                        text = json.dumps(
                            raw_out if raw_out is not None
                            else _("The action completed successfully."),
                            ensure_ascii=False,
                            default=str,
                        )
                        output = {"content": [{"type": "text", "text": text}], "isError": False}
                    else:
                        raw_out = bound(**incoming)
                        try:
                            output = dict(raw_out)
                            output.setdefault("content", [])
                            output["isError"] = False
                        except Exception as exc:
                            raise _CapabilityResultError from exc
                resp_summary = self._build_resp_summary(name, output, record_ids)
            except (ValueError, TypeError) as exc:
                _logger.warning(
                    "adv_mcp tool '%s' raised %s", name, type(exc).__name__, exc_info=True
                )
                error_detail = sanitizer.sanitize_message(str(exc))
                return _capability_error(error_detail)
            except _CapabilityResultError as exc:
                error_detail = sanitizer.sanitize_exception(
                    exc.__cause__,
                    "adv_mcp tool '%s' result post-processing failed" % name,
                )
                return _capability_error(error_detail)
            except OperationalError:
                concurrency_retry = True
                raise
            except AccessError as exc:
                rejected = True
                self._record_access_denied(name, operation, model_name)
                return _capability_error(sanitizer.sanitize_exception(exc))
            except Exception as exc:
                error_detail = sanitizer.sanitize_exception(
                    exc, "adv_mcp tool '%s' failed" % name
                )
                return _capability_error(error_detail)
            return output
        finally:
            if not rejected and not concurrency_retry:
                duration_ms = int((datetime.now() - ts).total_seconds() * 1000)
                self._record_tool_call(
                    tool_name=name,
                    model_name=model_name,
                    operation=operation,
                    error_detail=error_detail,
                    duration_ms=duration_ms,
                    record_ids=record_ids,
                    req_summary=req_summary,
                    resp_summary=resp_summary,
                )

    def _resolve_custom_tool(self, name: str):
        found = (
            request.env["adv.custom.tool"]
            .sudo()
            .search([("name", "=", name), ("active", "=", True)], limit=1)
        )
        if not found:
            self._reject_call(name, _("Unknown tool: %(name)s", name=name))
        ct = found.with_env(request.env)
        schema = found._parsed_input_schema()
        if schema is None:
            _logger.warning(
                "Custom tool '%s' has a malformed input_schema; treating it as empty.",
                found.name,
            )
            schema = {}
        return {"input_schema": schema, "annotations": {"readOnlyHint": found.is_readonly}}, ct

    @staticmethod
    def _extract_ids(incoming: dict):
        for key in ("record_ids", "ids"):
            value = incoming.get(key)
            if isinstance(value, (list, tuple)) and value:
                return list(value)
        single = incoming.get("record_id")
        return [single] if single is not None else None

    def _record_tool_call(
        self,
        tool_name,
        model_name,
        operation,
        error_detail,
        duration_ms,
        record_ids=None,
        req_summary=None,
        resp_summary=None,
    ):
        uid = request.env.uid
        ip = request.httprequest.remote_addr

        if error_detail:
            self._emit_audit(
                uid,
                lambda adv_log: adv_log.record_error(
                    error_message=error_detail,
                    error_code="E500",
                    endpoint="/mcp_server",
                    model_name=model_name,
                    operation=operation,
                    user_id=uid,
                    ip_address=ip,
                ),
                "adv_mcp audit logging failed for tool '%s'",
                tool_name,
            )
        else:
            try:
                request.env["adv.event"].sudo().record_access(
                    model_name=model_name,
                    operation=operation,
                    user_id=uid,
                    record_ids=record_ids,
                    endpoint="/mcp_server",
                    http_method="POST",
                    duration_ms=duration_ms,
                    ip_address=ip,
                    tool_name=tool_name,
                    request_data=req_summary,
                    response_data=resp_summary,
                    **self._auth_context(),
                )
            except Exception:
                _logger.exception("adv_mcp audit logging failed for tool '%s'", tool_name)

    @staticmethod
    def _auth_context() -> dict:
        scope = getattr(request, "_adv_oauth_scope", None)
        headers = request.httprequest.headers
        return {
            "auth_method": getattr(request, "_adv_auth_method", None),
            "oauth_client_id": getattr(request, "_adv_oauth_client_id", None),
            "oauth_scope": scope or None,
            "user_agent": headers.get("User-Agent"),
            "session_id": headers.get("Mcp-Session-Id"),
        }

    @staticmethod
    def _build_resp_summary(name: str, output: dict, record_ids) -> str | None:
        structured = output.get("structuredContent") if isinstance(output, dict) else None
        if isinstance(structured, dict):
            if name == "search_records" and "total" in structured:
                return "%s records" % structured["total"]
            if name == "aggregate_records" and "groups" in structured:
                return "%s groups" % len(structured["groups"])
        if record_ids:
            return "%s record(s)" % len(record_ids)
        return None

    def _emit_e400(self, tool_name, message: str):
        uid = request.env.uid
        op = tool_name if isinstance(tool_name, str) else str(tool_name)
        self._emit_audit(
            uid,
            lambda adv_log: adv_log.record_error(
                error_message=message,
                error_code="E400",
                endpoint="/mcp_server",
                operation=op,
                user_id=uid,
                ip_address=request.httprequest.remote_addr,
            ),
            "adv_mcp audit logging failed for rejected call '%s'",
            op,
        )

    def _reject_call(self, tool_name, message: str):
        self._emit_e400(tool_name, message)
        raise protocol.GatewayError(protocol.ERR_INVALID_PARAMS, message)

    def _bad_input(self, name: str, message: str) -> dict:
        self._emit_e400(name, message)
        return _capability_error(message)

    def _deny(self, name: str, operation, model_name, message: str) -> dict:
        self._record_access_denied(name, operation, model_name)
        return _capability_error(message)

    def _record_access_denied(self, name: str, operation, model_name):
        uid = request.env.uid
        ip = request.httprequest.remote_addr
        ctx = self._auth_context()
        self._emit_audit(
            uid,
            lambda adv_log: adv_log.record_access_denied(
                model_name=model_name,
                operation=operation,
                user_id=uid,
                endpoint="/mcp_server",
                ip_address=ip,
                **ctx,
            ),
            "adv_mcp permission-denied audit logging failed for tool '%s'",
            name,
        )

    @_handles("resources/templates/list")
    def _list_resource_templates(self, args: dict) -> dict:
        return {
            "resourceTemplates": [
                {
                    "uriTemplate": "odoo://record/{model}/{id}/{field}",
                    "name": "Odoo record binary field",
                    "description": (
                        "Fetch a binary/image field from an Odoo record "
                        "(e.g. an image or stored document) instead of inlining base64."
                    ),
                },
                {
                    "uriTemplate": "odoo://attachment/{id}",
                    "name": "Odoo attachment",
                    "description": "Fetch an ir.attachment by ID.",
                },
            ]
        }

    @_handles("resources/list")
    def _list_resources(self, args: dict) -> dict:
        return {"resources": []}

    @_handles("resources/read")
    def _fetch_resource(self, args: dict) -> dict:
        uri = args.get("uri")
        if not isinstance(uri, str) or not uri:
            self._reject_call("resources/read", _("Invalid params: 'uri' is required."))

        ts = datetime.now()
        error_detail = None
        concurrency_retry = False
        denied = False
        model_name = self._resource_model_hint(uri)
        try:
            try:
                entry = request.env["adv.tool.mixin"]._read_resource(uri)
            except AccessError as exc:
                denied = True
                self._record_access_denied("resources/read", "read", model_name)
                raise protocol.GatewayError(
                    protocol.ERR_FORBIDDEN, sanitizer.sanitize_exception(exc)
                ) from exc
            except sanitizer.SAFE_EXCEPTIONS as exc:
                error_detail = sanitizer.sanitize_exception(exc)
                raise protocol.GatewayError(protocol.ERR_INVALID_PARAMS, error_detail) from exc
            except OperationalError:
                concurrency_retry = True
                raise
            except Exception as exc:
                error_detail = sanitizer.sanitize_exception(
                    exc, "adv_mcp resources/read failed for %s" % uri
                )
                raise protocol.GatewayError(protocol.ERR_INTERNAL, error_detail) from None
            return {"contents": [entry]}
        finally:
            if not concurrency_retry and not denied:
                duration_ms = int((datetime.now() - ts).total_seconds() * 1000)
                self._record_tool_call(
                    tool_name="resources/read",
                    model_name=model_name,
                    operation="read",
                    error_detail=error_detail,
                    duration_ms=duration_ms,
                )

    @staticmethod
    def _resource_model_hint(uri: str) -> str | None:
        if uri.startswith("odoo://record/"):
            parts = uri[len("odoo://record/"):].split("/", 1)
            return parts[0] or None
        if uri.startswith("odoo://attachment/"):
            return "ir.attachment"
        return None

    def _notification_ack(self, args: dict) -> Response:
        return Response("", status=202)
