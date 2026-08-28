# Envelope helpers for the adv_mcp gateway.
# Internally uses typed error keys; wire serialization is JSON-RPC 2.0.

ERR_BAD_REQUEST = "bad_request"
ERR_NOT_FOUND = "not_found"
ERR_INVALID_PARAMS = "invalid_params"
ERR_INTERNAL = "internal_error"
ERR_SERVER = "server_error"
ERR_FORBIDDEN = "forbidden"

_WIRE_CODES: dict[str, int] = {
    ERR_BAD_REQUEST: -32600,
    ERR_NOT_FOUND: -32601,
    ERR_INVALID_PARAMS: -32602,
    ERR_INTERNAL: -32603,
    ERR_SERVER: -32000,
    ERR_FORBIDDEN: -32000,
}


class GatewayError(Exception):
    def __init__(self, error_type: str, detail: str, data=None):
        super().__init__(detail)
        self.error_type = error_type
        self.detail = detail
        self.data = data


def parse_envelope(raw: dict) -> tuple:
    """Extract (method, args, ref) from a JSON-RPC 2.0 body."""
    if not isinstance(raw, dict):
        raise GatewayError(ERR_BAD_REQUEST, "Request body must be a JSON object")
    if raw.get("jsonrpc") != "2.0":
        raise GatewayError(ERR_BAD_REQUEST, "Unexpected envelope format")
    method = raw.get("method")
    if not isinstance(method, str) or not method:
        raise GatewayError(ERR_BAD_REQUEST, "Field 'method' must be a non-empty string")
    return method, raw.get("params") or {}, raw.get("id")


def wrap_ok(data: dict, *, ref=None) -> dict:
    return {"jsonrpc": "2.0", "id": ref, "result": data}


def wrap_err(error_type: str, detail: str, *, ref=None, hint=None) -> dict:
    code = _WIRE_CODES.get(error_type, -32000)
    body: dict = {"code": code, "message": detail}
    if hint is not None:
        body["data"] = hint
    return {"jsonrpc": "2.0", "id": ref, "error": body}
