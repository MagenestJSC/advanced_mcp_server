# Issued OAuth 2.1 access/refresh tokens for the Adv MCP endpoint.

import logging
import uuid
from datetime import timedelta

from authlib.oauth2.rfc6749 import TokenMixin
from authlib.oauth2.rfc6749.errors import InvalidRequestError

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

from ..server.oauth.discovery import resource_url
from .hash_utils import sha256_hex as _sha256

_logger = logging.getLogger(__name__)

_REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600
_DCR_CLIENT_TTL_DAYS = 30
_GC_BATCH_SIZE = 1000


class AdvOauthToken(models.Model, TokenMixin):
    _name = "adv.oauth.token"
    _description = "Adv MCP OAuth Token"
    _rec_name = "client"
    _order = "create_date desc"

    access_token_hash = fields.Char(
        string="Access Token Hash",
        index=True,
        copy=False,
        help="SHA-256 hash of the access token; the raw token is never stored.",
    )
    refresh_token_hash = fields.Char(
        string="Refresh Token Hash",
        index=True,
        copy=False,
        help="SHA-256 hash of the refresh token; the raw token is never stored.",
    )
    client = fields.Many2one(
        "adv.oauth.client",
        string="Client",
        required=True,
        index=True,
        ondelete="cascade",
    )
    user_id = fields.Many2one(
        "res.users",
        string="User",
        required=True,
        index=True,
        ondelete="cascade",
    )
    scope = fields.Char(string="Scope")
    audience = fields.Char(string="Audience")
    access_expires_at = fields.Datetime(string="Access Token Expiry", index=True)
    refresh_expires_at = fields.Datetime(string="Refresh Token Expiry", index=True)
    revoked = fields.Boolean(default=False, copy=False, index=True)
    refresh_family_id = fields.Char(string="Refresh Family", index=True, copy=False)
    create_date = fields.Datetime(index=True)

    @api.model
    def _resolve_audience_and_family(self, oauth2_request):
        old_refresh = getattr(oauth2_request, "refresh_token", None)
        auth_code = getattr(oauth2_request, "authorization_code", None)
        requested_resource = oauth2_request.payload.data.get("resource")
        if auth_code is not None:
            code_resource = auth_code.resource
            if (
                code_resource
                and requested_resource
                and requested_resource.rstrip("/") != code_resource.rstrip("/")
            ):
                raise InvalidRequestError(
                    "The 'resource' does not match the authorization request."
                )
            audience = code_resource or requested_resource or resource_url()
        elif old_refresh is not None:
            audience = old_refresh.audience
        else:
            audience = requested_resource or resource_url()
        family_id = old_refresh.refresh_family_id if old_refresh else uuid.uuid4().hex
        return audience, family_id

    @api.model
    def _save_token(self, token, oauth2_request):
        now = fields.Datetime.now()
        auth_code = getattr(oauth2_request, "authorization_code", None)
        old_refresh = getattr(oauth2_request, "refresh_token", None)
        audience, family_id = self._resolve_audience_and_family(oauth2_request)
        if auth_code is not None:
            scope = auth_code.scope or ""
        elif old_refresh is not None:
            scope = old_refresh.scope or ""
        else:
            scope = token.get("scope") or ""
        vals = {
            "access_token_hash": _sha256(token["access_token"]),
            "client": oauth2_request.client.id,
            "user_id": oauth2_request.user.id,
            "scope": scope,
            "audience": audience,
            "access_expires_at": now + timedelta(seconds=token.get("expires_in", 0)),
            "refresh_family_id": family_id,
        }
        refresh_token = token.get("refresh_token")
        if refresh_token:
            vals["refresh_token_hash"] = _sha256(refresh_token)
            vals["refresh_expires_at"] = now + timedelta(
                seconds=_REFRESH_TOKEN_TTL_SECONDS
            )
        new_token = self.create(vals)
        if auth_code is not None:
            auth_code.refresh_family_id = family_id
        return new_token

    @api.model
    def _get_valid_access_token(self, access_token):
        return self.search(
            [
                ("access_token_hash", "=", _sha256(access_token)),
                ("revoked", "=", False),
                ("access_expires_at", ">", fields.Datetime.now()),
            ],
            limit=1,
        )

    @api.model
    def _get_valid_refresh_token(self, refresh_token):
        self.env.cr.execute(
            """
            SELECT id FROM adv_oauth_token
            WHERE refresh_token_hash = %s
              AND revoked = FALSE AND refresh_expires_at > %s
            LIMIT 1
            FOR UPDATE
            """,
            (_sha256(refresh_token), fields.Datetime.now()),
        )
        row = self.env.cr.fetchone()
        return self.browse(row[0]) if row else self.browse()

    def _revoke(self):
        self.write({"revoked": True})

    @api.model
    def _revoke_family(self, family_id):
        if not family_id:
            return
        self.search(
            [("refresh_family_id", "=", family_id), ("revoked", "=", False)]
        )._revoke()

    @api.model
    def _detect_refresh_reuse(self, refresh_token):
        spent = self.search(
            [
                ("refresh_token_hash", "=", _sha256(refresh_token)),
                ("revoked", "=", True),
            ],
            limit=1,
        )
        if spent:
            self._revoke_family(spent.refresh_family_id)

    def action_revoke(self):
        # Admin action: revoke the selected token(s).
        if not self.env.user.has_group("advanced_mcp_server.group_adv_admin"):
            raise AccessError(_("Only Adv MCP administrators may revoke OAuth tokens."))
        self.sudo()._revoke()

    @api.model
    def _gc_oauth(self):
        # Garbage-collect spent OAuth credentials (run from ir.cron).
        now = fields.Datetime.now()
        live_tokens = self.search(
            [
                ("revoked", "=", False),
                "|",
                ("access_expires_at", ">", now),
                ("refresh_expires_at", ">", now),
            ]
        )
        live_family_ids = set(live_tokens.mapped("refresh_family_id"))
        code_model = self.env["adv.oauth.code"]
        code_count = 0
        last_id = 0
        while True:
            batch = code_model.search(
                [("expires_at", "<", now), ("id", ">", last_id)],
                limit=_GC_BATCH_SIZE,
                order="id",
            )
            if not batch:
                break
            last_id = batch[-1].id
            to_delete = batch.filtered(
                lambda c: not (
                    c.used
                    and c.refresh_family_id
                    and c.refresh_family_id in live_family_ids
                )
            )
            code_count += len(to_delete)
            to_delete.sudo().unlink()
        token_count = self._gc_unlink_batched(
            self.env["adv.oauth.token"],
            [
                ("access_expires_at", "<", now),
                "|",
                ("refresh_expires_at", "=", False),
                ("refresh_expires_at", "<", now),
            ],
        )
        client_count = self._gc_unlink_batched(
            self.env["adv.oauth.client"].with_context(active_test=False),
            [
                ("created_via", "=", "dcr"),
                ("create_date", "<", now - timedelta(days=_DCR_CLIENT_TTL_DAYS)),
                ("id", "not in", live_tokens.client.ids),
            ],
        )
        counts = (code_count, token_count, client_count)
        _logger.info(
            "Adv MCP OAuth GC removed %s authorization codes, %s tokens, %s stale clients",
            *counts,
        )
        return counts

    @staticmethod
    def _gc_unlink_batched(model, domain, batch_size=_GC_BATCH_SIZE):
        count = 0
        while True:
            batch = model.search(domain, limit=batch_size)
            if not batch:
                break
            count += len(batch)
            batch.sudo().unlink()
        return count

    def check_client(self, client):
        return self.client.id == client.id

    def get_scope(self):
        return self.scope or ""

    def get_expires_in(self):
        if not self.access_expires_at:
            return 0
        return int((self.access_expires_at - fields.Datetime.now()).total_seconds())

    def is_expired(self):
        if not self.access_expires_at:
            return False
        return self.access_expires_at < fields.Datetime.now()

    def is_revoked(self):
        return self.revoked

    def get_user(self):
        return self.user_id

    def get_client(self):
        return self.client
