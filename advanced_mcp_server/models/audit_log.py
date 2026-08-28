import logging
from datetime import timedelta

from odoo import SUPERUSER_ID, api, fields, models

_logger = logging.getLogger(__name__)

_MAX_TEXT = 10000
_MAX_CHAR = 256
_TEXT_COLS = ("request_data", "response_data", "error_message", "user_agent")
_CHAR_COLS = ("resource_name", "capability_name", "operation")

_EVENT_CLASS = {
    "auth_success": "security",
    "auth_failure": "security",
    "permission_denied": "security",
    "data_access": "data",
    "resource_fetch": "data",
    "mutation": "data",
    "error": "ops",
    "quota_exceeded": "ops",
}

_RISK_SCORES = {
    "auth_failure": 8,
    "permission_denied": 6,
    "quota_exceeded": 4,
    "error": 3,
    "mutation": 2,
    "data_access": 1,
    "auth_success": 0,
    "resource_fetch": 1,
}


def _clip(value, limit, suffix="… [clipped]"):
    s = str(value)
    return s if len(s) <= limit else s[:limit] + suffix


class AdvEventLog(models.Model):
    _name = "adv.event"
    _description = "Adv MCP Event Log"
    _order = "id desc"
    _rec_name = "event_kind"

    create_date = fields.Datetime(index=True)

    event_kind = fields.Selection(
        [
            ("auth_success", "Auth OK"),
            ("auth_failure", "Auth Failure"),
            ("data_access", "Data Access"),
            ("resource_fetch", "Resource Fetch"),
            ("mutation", "Mutation"),
            ("error", "Error"),
            ("quota_exceeded", "Quota Exceeded"),
            ("permission_denied", "Permission Denied"),
        ],
        required=True,
        index=True,
        string="Event",
    )
    event_class = fields.Selection(
        [("security", "Security"), ("data", "Data"), ("ops", "Operations")],
        string="Class",
        index=True,
    )
    risk_score = fields.Integer(default=0, index=True, string="Risk (0–10)")

    user_id = fields.Many2one("res.users", string="User", index=True)
    ip_address = fields.Char(size=45)
    session_id = fields.Char(index=True, string="Session")

    auth_method = fields.Selection(
        [("api_key", "API Key"), ("oauth", "OAuth"), ("session", "Session")],
        index=True,
    )
    oauth_client_id = fields.Many2one(
        "adv.oauth.client", string="OAuth Client", ondelete="set null", index=True
    )
    oauth_scope = fields.Char()

    endpoint = fields.Char(index=True)
    http_method = fields.Char()
    resource_name = fields.Char(string="Resource", index=True)
    operation = fields.Char(index=True)
    capability_name = fields.Char(string="Capability", index=True)
    record_ids = fields.Char(string="Record IDs")

    request_data = fields.Text()
    response_data = fields.Text()
    error_message = fields.Text()
    error_code = fields.Char()
    duration_ms = fields.Integer(string="Duration (ms)")
    user_agent = fields.Text()

    def _logging_active(self):
        return self.env["adv.server.config"].sudo()._get_config().enable_logging

    @api.model
    def record_event(self, event_kind: str, **kw):
        if not self._logging_active():
            return self.env["adv.event"]
        if self.env.context.get("skip_adv_logging"):
            return self.env["adv.event"]
        if getattr(self.env.cr, "readonly", False):
            return self.env["adv.event"]

        entry = {
            "event_kind": event_kind,
            "event_class": _EVENT_CLASS.get(event_kind, "ops"),
            "risk_score": _RISK_SCORES.get(event_kind, 0),
            "user_id": kw.get("user_id",
                self.env.user.id if self.env.user.id != SUPERUSER_ID else False),
            "ip_address": kw.get("ip_address"),
            "session_id": kw.get("session_id"),
            "auth_method": kw.get("auth_method"),
            "oauth_client_id": kw.get("oauth_client_id"),
            "oauth_scope": kw.get("oauth_scope"),
            "endpoint": kw.get("endpoint"),
            "http_method": kw.get("http_method"),
            "resource_name": kw.get("model_name"),
            "operation": kw.get("operation"),
            "capability_name": kw.get("tool_name"),
            "record_ids": kw.get("record_ids"),
            "request_data": kw.get("request_data"),
            "response_data": kw.get("response_data"),
            "error_message": kw.get("error_message"),
            "error_code": kw.get("error_code"),
            "duration_ms": kw.get("duration_ms"),
            "user_agent": kw.get("user_agent"),
        }
        for col in _TEXT_COLS:
            if entry.get(col):
                entry[col] = _clip(entry[col], _MAX_TEXT)
        for col in _CHAR_COLS:
            if entry.get(col):
                entry[col] = _clip(entry[col], _MAX_CHAR)
        try:
            with self.env.cr.savepoint():
                return self.sudo().create(entry)
        except Exception as exc:
            _logger.error("Failed to persist event log entry: %s", exc)
            return self.env["adv.event"]

    @api.model
    def record_access(self, model_name, operation, user_id=None, record_ids=None,
                      endpoint=None, http_method=None, duration_ms=None, ip_address=None,
                      tool_name=None, request_data=None, response_data=None, session_id=None,
                      user_agent=None, auth_method=None, oauth_client_id=None, oauth_scope=None):
        ids_str = ",".join(map(str, record_ids)) if record_ids else None
        return self.record_event(
            "data_access", model_name=model_name, operation=operation, user_id=user_id,
            record_ids=ids_str, endpoint=endpoint, http_method=http_method,
            duration_ms=duration_ms, ip_address=ip_address, tool_name=tool_name,
            request_data=request_data, response_data=response_data, session_id=session_id,
            user_agent=user_agent, auth_method=auth_method, oauth_client_id=oauth_client_id,
            oauth_scope=oauth_scope,
        )

    @api.model
    def record_error(self, error_message, error_code=None, endpoint=None, model_name=None,
                     operation=None, user_id=None, ip_address=None, request_data=None):
        return self.record_event(
            "error", error_message=error_message, error_code=error_code, endpoint=endpoint,
            model_name=model_name, operation=operation, user_id=user_id,
            ip_address=ip_address, request_data=request_data,
        )

    @api.model
    def record_quota_exceeded(self, user_id, endpoint=None, ip_address=None):
        return self.record_event(
            "quota_exceeded", user_id=user_id, endpoint=endpoint, ip_address=ip_address,
            error_message="Request quota exceeded",
        )

    @api.model
    def record_access_denied(self, model_name, operation, user_id=None, endpoint=None,
                              ip_address=None, error_message=None, auth_method=None,
                              oauth_client_id=None, oauth_scope=None, user_agent=None,
                              session_id=None):
        return self.record_event(
            "permission_denied", model_name=model_name, operation=operation, user_id=user_id,
            endpoint=endpoint, ip_address=ip_address,
            error_message=error_message or f"Access denied for {operation} on {model_name}",
            auth_method=auth_method, oauth_client_id=oauth_client_id, oauth_scope=oauth_scope,
            user_agent=user_agent, session_id=session_id,
        )

    @api.model
    def purge_old_entries(self, days=None):
        if days is None:
            days = self.env["adv.server.config"].sudo()._get_config().log_retention_days
        if days <= 0:
            return 0
        cutoff = fields.Datetime.now() - timedelta(days=days)
        total = 0
        while True:
            batch = self.search([("create_date", "<", cutoff)], limit=1000)
            if not batch:
                break
            total += len(batch)
            batch.unlink()
        _logger.info("Purged %s event log entries older than %s days", total, days)
        return total

    @api.depends("event_kind", "resource_name", "operation")
    def _compute_display_name(self):
        labels = dict(self._fields["event_kind"].selection)
        for rec in self:
            parts = [labels.get(rec.event_kind, "")]
            if rec.resource_name:
                parts.append(rec.resource_name)
            if rec.operation:
                parts.append(rec.operation)
            rec.display_name = " — ".join(filter(None, parts))
