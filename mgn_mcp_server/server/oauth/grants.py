# Authlib bridge classes for the adv_mcp OAuth 2.1 server

import json
import logging
import time
from collections import defaultdict
from functools import cached_property
from urllib.parse import urlsplit

from authlib.common.security import generate_token
from authlib.oauth2 import AuthorizationServer, OAuth2Error
from authlib.oauth2.rfc6749 import (
    InvalidGrantError,
    JsonPayload,
    JsonRequest,
    OAuth2Payload,
    OAuth2Request,
    grants,
)
from authlib.oauth2.rfc6750 import BearerTokenGenerator
from authlib.oauth2.rfc7591 import ClientRegistrationEndpoint, InvalidClientMetadataError
from authlib.oauth2.rfc7636 import CodeChallenge

from odoo.http import Response, request

_logger = logging.getLogger(__name__)

ACCESS_TOKEN_EXPIRES_IN = 3600

SUPPORTED_GRANT_TYPES = ["authorization_code", "refresh_token"]
SUPPORTED_RESPONSE_TYPES = ["code"]
SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS = ["none"]

ADV_SCOPES = ["adv", "adv:read", "adv:write"]
_ADV_WRITE_SCOPES = frozenset({"adv", "adv:write"})

# Map legacy / alternate scope prefixes to canonical adv:* names.
_SCOPE_ALIASES = {
    "amcp": "adv", "amcp:read": "adv:read", "amcp:write": "adv:write",
    "mcp": "adv", "mcp:read": "adv:read", "mcp:write": "adv:write",
    "mcp_server": "adv", "mcp_server:read": "adv:read", "mcp_server:write": "adv:write",
}


def normalize_scope(scope_str):
    if not scope_str:
        return scope_str
    return " ".join(_SCOPE_ALIASES.get(s, s) for s in scope_str.split())


def grants_write(scope):
    return bool(_ADV_WRITE_SCOPES & set(normalize_scope(scope or "").split()))


def read_only_scope(scope_str):
    return set(normalize_scope(scope_str or "").split()) == {"adv:read"}


def expand_scopes(scope):
    granted = set(normalize_scope(scope or "").split())
    if "adv" in granted:
        granted |= {"adv:read", "adv:write"}
    if "adv:write" in granted:
        granted.add("adv:read")
    return granted


def client_write_allowed(client):
    registered = client.scope or ""
    return not registered or grants_write(registered)


class _OdooOAuth2Payload(OAuth2Payload):
    def __init__(self, httprequest):
        self._httprequest = httprequest

    @property
    def data(self):
        req = self._httprequest
        return req.form if req.method == "POST" else req.args

    @cached_property
    def datalist(self):
        values = defaultdict(list)
        for key in self.data:
            values[key].extend(self.data.getlist(key))
        return values


class OdooTokenRequest(OAuth2Request):
    def __init__(self, httprequest):
        super().__init__(
            method=httprequest.method,
            uri=httprequest.url,
            headers=httprequest.headers,
        )
        self._httprequest = httprequest
        self.payload = _OdooOAuth2Payload(httprequest)

    @property
    def form(self):
        return self._httprequest.form


class _OdooJsonPayload(JsonPayload):
    def __init__(self, httprequest):
        self._httprequest = httprequest

    @property
    def data(self):
        return self._httprequest.get_json()


class OdooJsonRequest(JsonRequest):
    def __init__(self, httprequest):
        super().__init__(httprequest.method, httprequest.url, httprequest.headers)
        self.payload = _OdooJsonPayload(httprequest)


class S256Verifier(CodeChallenge):
    SUPPORTED_CODE_CHALLENGE_METHOD = ["S256"]
    DEFAULT_CODE_CHALLENGE_METHOD = "S256"

    def validate_code_verifier(self, grant, result):
        authorization_code = grant.request.authorization_code
        if authorization_code is not None:
            method = self.get_authorization_code_challenge_method(authorization_code)
            if method and method not in self.SUPPORTED_CODE_CHALLENGE_METHOD:
                raise InvalidGrantError(description="Unsupported code_challenge_method.")
        return super().validate_code_verifier(grant, result)


class PKCECodeGrant(grants.AuthorizationCodeGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]

    def save_authorization_code(self, code, oauth2_request):
        codes = request.env["adv.oauth.code"].sudo()
        return codes._save_code(code, oauth2_request)

    def query_authorization_code(self, code, client):
        codes = request.env["adv.oauth.code"].sudo()
        authorization_code = codes._get_valid_code(code, client)
        if authorization_code:
            return authorization_code
        codes._detect_reuse(code)
        return None

    def delete_authorization_code(self, authorization_code):
        authorization_code.sudo()._consume()

    def authenticate_user(self, authorization_code):
        user = authorization_code.sudo().user_id
        return user if user and user.active else None


class RotatingRefreshGrant(grants.RefreshTokenGrant):
    TOKEN_ENDPOINT_AUTH_METHODS = ["none"]
    INCLUDE_NEW_REFRESH_TOKEN = True

    def authenticate_refresh_token(self, refresh_token):
        tokens = request.env["adv.oauth.token"].sudo()
        valid = tokens._get_valid_refresh_token(refresh_token)
        if valid:
            return valid
        tokens._detect_refresh_reuse(refresh_token)
        return None

    def authenticate_user(self, credential):
        user = credential.sudo().user_id
        return user if user and user.active else None

    def revoke_old_credential(self, refresh_token):
        refresh_token.sudo()._revoke()


_MAX_REDIRECT_URIS = 5
_MAX_REDIRECT_URI_LENGTH = 2048
_MAX_CLIENT_STRING_LENGTH = 255

_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def _is_allowed_redirect_uri(uri):
    try:
        parts = urlsplit(uri)
        username = parts.username
        hostname = parts.hostname
        port = parts.port
    except ValueError:
        return False
    if username is not None:
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    if parts.fragment:
        return False
    if parts.scheme == "https" and hostname:
        return True
    if parts.scheme == "http" and hostname in _LOOPBACK_HOSTS:
        return True
    return False


class DynamicRegistration(ClientRegistrationEndpoint):
    def authenticate_token(self, oauth2_request):
        return True

    def get_server_metadata(self):
        return {
            "grant_types_supported": SUPPORTED_GRANT_TYPES,
            "response_types_supported": SUPPORTED_RESPONSE_TYPES,
            "token_endpoint_auth_methods_supported": SUPPORTED_TOKEN_ENDPOINT_AUTH_METHODS,
        }

    def generate_client_info(self, oauth_request):
        return {
            "client_id": self.generate_client_id(oauth_request),
            "client_id_issued_at": int(time.time()),
        }

    def extract_client_metadata(self, oauth_request):
        data = oauth_request.payload.data
        if isinstance(data, dict) and not data.get("token_endpoint_auth_method"):
            data["token_endpoint_auth_method"] = "none"  # nosec B105
        metadata = super().extract_client_metadata(oauth_request)
        metadata["token_endpoint_auth_method"] = "none"  # nosec B105
        metadata["grant_types"] = SUPPORTED_GRANT_TYPES
        metadata["response_types"] = SUPPORTED_RESPONSE_TYPES
        return metadata

    def save_client(self, client_info, client_metadata, oauth2_request):
        redirect_uris = client_metadata.get("redirect_uris") or []
        if not redirect_uris:
            raise InvalidClientMetadataError("At least one redirect_uri is required.")
        if len(redirect_uris) > _MAX_REDIRECT_URIS:
            raise InvalidClientMetadataError(
                "Too many redirect_uris (max %d)." % _MAX_REDIRECT_URIS
            )
        for uri in redirect_uris:
            if len(uri) > _MAX_REDIRECT_URI_LENGTH:
                raise InvalidClientMetadataError(
                    "A redirect_uri is too long (max %d characters)." % _MAX_REDIRECT_URI_LENGTH
                )
            if not _is_allowed_redirect_uri(uri):
                raise InvalidClientMetadataError(
                    "Each redirect_uri must use https, or http for loopback only "
                    "127.0.0.1 or localhost."
                )
        if len(client_metadata.get("client_name") or "") > _MAX_CLIENT_STRING_LENGTH:
            raise InvalidClientMetadataError(
                "client_name is too long (max %d characters)." % _MAX_CLIENT_STRING_LENGTH
            )
        raw_scope = client_metadata.get("scope") or ""
        if len(raw_scope) > _MAX_CLIENT_STRING_LENGTH:
            raise InvalidClientMetadataError(
                "scope is too long (max %d characters)." % _MAX_CLIENT_STRING_LENGTH
            )
        client_metadata["scope"] = normalize_scope(raw_scope)
        clients = request.env["adv.oauth.client"].sudo()
        return clients._register_client(client_info, client_metadata)


class AdvAuthServer(AuthorizationServer):
    def send_signal(self, name, *args, **kwargs):
        pass

    def validate_requested_scope(self, scope):
        return super().validate_requested_scope(normalize_scope(scope))

    def create_oauth2_request(self, framework_request):
        return OdooTokenRequest(request.httprequest)

    def create_json_request(self, framework_request):
        return OdooJsonRequest(request.httprequest)

    def handle_response(self, status, body, headers):
        if isinstance(body, dict):
            body = json.dumps(body)
        return Response(body, status=status, headers=headers)

    def query_client(self, client_id):
        clients = request.env["adv.oauth.client"].sudo()
        domain = [("client_id", "=", client_id), ("active", "=", True)]
        return clients.search(domain, limit=1) or None

    def save_token(self, token, oauth2_request):
        tokens = request.env["adv.oauth.token"].sudo()
        tokens._save_token(token, oauth2_request)


def opaque_token(**kwargs):
    return generate_token(48)


def _build_auth_server():
    server = AdvAuthServer()
    server.scopes_supported = ADV_SCOPES
    server.register_token_generator(
        "default",
        BearerTokenGenerator(
            access_token_generator=opaque_token,
            refresh_token_generator=opaque_token,
            expires_generator=ACCESS_TOKEN_EXPIRES_IN,
        ),
    )
    server.register_grant(PKCECodeGrant, [S256Verifier(required=True)])
    server.register_grant(RotatingRefreshGrant)
    server.register_endpoint(DynamicRegistration)
    return server


_auth_server = None


def get_auth_server():
    global _auth_server
    if _auth_server is None:
        _auth_server = _build_auth_server()
    return _auth_server
