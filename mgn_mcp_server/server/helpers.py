import logging
import re
from datetime import datetime, timezone
from typing import Dict, List, Optional, Union

import odoo
from odoo import _, modules
from odoo.api import Environment
from odoo.http import request
from odoo.modules.registry import Registry

_logger = logging.getLogger(__name__)


# --- cache helpers ---

def clear_adv_caches() -> None:
    try:
        request.env.registry.clear_cache()
    except Exception:
        pass
    for registry in list(Registry.registries.values()):
        registry.clear_cache()


# --- model name validation ---

def sanitize_model_name(model_name: str) -> str:
    if not model_name:
        raise ValueError("Model name cannot be empty")
    if not re.match(r"^[a-zA-Z0-9._]+$", model_name):
        raise ValueError(f"Invalid model name format: {model_name}")
    return model_name.strip()


def _sanitize_or_log(model_name: str, log_prefix: str = "Invalid model name") -> Optional[str]:
    try:
        return sanitize_model_name(model_name)
    except ValueError as e:
        _logger.warning("%s: %s", log_prefix, e)
        return None


# --- server-level gates ---

def is_adv_server_enabled() -> bool:
    try:
        return request.env["adv.module.access"].sudo()._get_amcp_enabled()
    except Exception as exc:
        _logger.error("Error checking gateway enabled state: %s", exc)
        return False


def get_allowed_origins() -> tuple:
    try:
        return request.env["adv.module.access"].sudo()._get_allowed_origins()
    except Exception as exc:
        _logger.error("Error reading Origin allowlist: %s", exc)
        return ()


def is_adv_oauth_enabled(env: Environment) -> bool:
    try:
        return env["adv.module.access"].sudo()._get_oauth_enabled()
    except Exception as exc:
        _logger.error("Error checking OAuth state: %s", exc)
        return False


# --- model access checks ---

def is_model_accessible(env: Environment, model_name: str) -> bool:
    if not is_adv_server_enabled():
        return False

    model_name = _sanitize_or_log(model_name)
    if model_name is None:
        return False

    try:
        access = env["adv.module.access"].sudo()
        return access.is_model_enabled(model_name)
    except Exception as e:
        _logger.error(f"Error checking if model {model_name} is accessible: {e}")
        return False


def verify_model_operation(env: Environment, model_name: str, operation: str) -> bool:
    if not is_adv_server_enabled():
        return False

    model_name = _sanitize_or_log(model_name)
    if model_name is None:
        return False

    operation = str(operation).strip().lower()
    valid_operations = ["read", "create", "write", "unlink"]
    if operation not in valid_operations:
        _logger.warning(f"Invalid operation '{operation}' requested for model '{model_name}'")
        return False

    if not is_model_accessible(env, model_name):
        return False

    try:
        access = env["adv.module.access"].sudo()
        return access.check_model_operation_enabled(model_name, operation)
    except Exception as e:
        _logger.error(f"Error checking operation {operation} for model {model_name}: {e}")
        return False


def verify_model_method(env: Environment, model_name: str) -> bool:
    if not is_adv_server_enabled():
        return False

    model_name = _sanitize_or_log(model_name)
    if model_name is None:
        return False

    if not is_model_accessible(env, model_name):
        return False

    try:
        access = env["adv.module.access"].sudo()
        return access.is_method_call_enabled(model_name)
    except Exception as e:
        _logger.error(f"Error checking method calls for model {model_name}: {e}")
        return False


def list_accessible_models(env: Environment) -> List[Dict]:
    if not is_adv_server_enabled():
        return []

    try:
        records = env["adv.module.access"].sudo().search([("active", "=", True)])
        if not records:
            return []
        enabled_modules = {rec.module_name for rec in records if rec.module_name}
        ir_models = env["ir.model"].sudo().search([("transient", "=", False)])
        access = env["adv.module.access"].sudo()
        result = []
        for m in ir_models:
            mods = [s.strip() for s in (m.modules or "").split(",") if s.strip()]
            if not any(mn in enabled_modules for mn in mods):
                continue
            result.append({
                "model": m.model,
                "name": m.name,
                "operations": {
                    op: access.check_model_operation_enabled(m.model, op)
                    for op in ("read", "create", "write", "unlink")
                },
            })
        return result
    except Exception as e:
        _logger.error(f"Error fetching accessible models: {e}")
        return []


XMLRPC_METHOD_OPERATION_MAP = {
    "read": "read",
    "search": "read",
    "search_read": "read",
    "search_count": "read",
    "name_search": "read",
    "fields_get": "read",
    "export_data": "read",
    "default_get": "read",
    "name_get": "read",
    "get_metadata": "read",
    "get_formview_id": "read",
    "get_formview_action": "read",
    "read_group": "read",
    "formatted_read_group": "read",
    "create": "create",
    "copy": "create",
    "name_create": "create",
    "write": "write",
    "toggle_active": "write",
    "action_archive": "write",
    "action_unarchive": "write",
    "message_post": "write",
    "unlink": "unlink",
    "action_delete": "unlink",
}


def map_method_to_operation(method: str) -> Optional[str]:
    return XMLRPC_METHOD_OPERATION_MAP.get(method)


def server_version() -> str:
    try:
        manifest = modules.module.get_manifest("mgn_mcp_server")
        return manifest.get("version", "1.0.0")
    except Exception as e:
        _logger.error(f"Error retrieving adv_mcp server version: {e}")
        return "1.0.0"


def system_info(env: Environment) -> Dict[str, Union[str, int]]:
    db_name = env.cr.dbname
    odoo_version = odoo.release.version

    lang = env.context.get("lang")
    if not lang and env.user:
        lang = env.user.lang
    if not lang:
        lang = env["ir.config_parameter"].sudo().get_param("base.language", "en_US")

    enabled_count = 0
    if is_adv_server_enabled():
        enabled_count = len(list_accessible_models(env))

    server_timezone = env.context.get("tz")
    if not server_timezone and env.user:
        server_timezone = env.user.tz
    if not server_timezone:
        server_timezone = "UTC"

    return {
        "db_name": db_name,
        "odoo_version": odoo_version,
        "language": lang,
        "enabled_adv_models": enabled_count,
        "adv_server_version": server_version(),
        "server_timezone": server_timezone,
    }


def _one_line(value: str) -> str:
    return str(value).replace("\r", " ").replace("\n", " ")


def describe_session(env: Environment) -> str:
    try:
        user = env.user
        timezone_line = user.tz or "UTC (user has no timezone set)"
        company = user.company_id
        lines = [
            _("You are connected to Odoo via Advanced MCP Server as:"),
            _(
                "- User: %(name)s (login: %(login)s)",
                name=_one_line(user.display_name),
                login=_one_line(user.login),
            ),
            _("- Timezone: %(tz)s", tz=timezone_line),
            _(
                "- Active company: %(name)s (ID: %(id)s)",
                name=_one_line(company.display_name),
                id=company.id,
            ),
        ]
        allowed_companies = user.company_ids
        if len(allowed_companies) > 1:
            names = ", ".join(
                f"{_one_line(c.display_name)} (ID: {c.id})" for c in allowed_companies
            )
            lines.append(_("- Allowed companies: %(names)s", names=names))
        lines.append("")
        lines.append(
            _(
                "Datetime handling:\n"
                "- All datetimes stored and returned by Odoo are in UTC.\n"
                "- Provide datetimes to tools in UTC.\n"
                "- Convert to the user's timezone only for display."
            )
        )
        return "\n".join(lines)
    except Exception as e:
        _logger.error(f"Error building adv_mcp session context: {e}")
        return _(
            "Datetime handling:\n"
            "- All datetimes stored and returned by Odoo are in UTC.\n"
            "- Provide datetimes to tools in UTC.\n"
            "- Convert to the user's timezone only for display."
        )


# --- HTTP response helpers (used by gateway and auth_resolver) ---

_ERROR_CODES = {
    400: "E400",
    401: "E401",
    403: "E403",
    404: "E404",
    429: "E429",
    500: "E500",
    503: "E503",
}


def _get_timestamp():
    return datetime.now(timezone.utc).isoformat()


def success_response(data, meta=None):
    if data is None:
        data = {}
    elif not isinstance(data, dict):
        try:
            data = dict(data)
        except (TypeError, ValueError):
            data = {"result": data}

    response_meta = {"timestamp": _get_timestamp()}
    if meta and isinstance(meta, dict):
        response_meta.update(meta)

    payload = {"success": True, "data": data, "meta": response_meta}
    return request.make_json_response(payload, headers={"Content-Type": "application/json"})


def error_response(message, code=None, status=400, meta=None):
    message = str(message) if message else "Unknown error"
    if not code:
        code = _ERROR_CODES.get(status, f"E{status}")

    response_meta = {"timestamp": _get_timestamp()}
    if meta and isinstance(meta, dict):
        response_meta.update(meta)

    payload = {
        "success": False,
        "error": {"message": message, "code": code},
        "meta": response_meta,
    }
    return request.make_json_response(
        payload, status=status, headers={"Content-Type": "application/json"}
    )
