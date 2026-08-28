# Advanced MCP Server — Workflow & Architecture

This document explains the entire `advanced_mcp_server` module: request processing flow, each model, and each important function.

---

## 1. Directory Structure

```
advanced_mcp_server/
├── models/              # Odoo models (ORM)
│   ├── module_access.py     → adv.module.access (module-based permission layer)
│   ├── audit_log.py         → adv.event
│   ├── config.py            → adv.server.config (singleton)
│   ├── custom_tool.py       → adv.custom.tool
│   ├── oauth_client.py      → adv.oauth.client
│   ├── oauth_token.py       → adv.oauth.token
│   ├── oauth_code.py        → adv.oauth.code
│   ├── http_layer.py        → ir.http (auth method)
│   ├── tool_mixin.py        → adv.tool.mixin (base tools)
│   ├── read_tools.py        → 9 read tools
│   ├── write_tools.py       → 6 write tools
│   └── apikeys.py           → res.users.apikeys (extend)
├── server/              # HTTP layer (not Odoo models)
│   ├── handler.py           → AdvMCPHandler (controller)
│   ├── dispatcher.py        → AdvHttpGateway (routing type)
│   ├── gateway.py           → REST endpoints (/health, /models…)
│   ├── auth_resolver.py     → API key / OAuth authentication
│   ├── audit_writer.py      → write events outside transaction
│   ├── protocol.py          → JSON-RPC helpers
│   ├── rate_limiter.py      → sliding window throttle
│   ├── rpc_proxy.py         → XML-RPC proxy
│   ├── helpers.py           → cache helpers
│   ├── sanitizer.py         → sanitize errors returned to client
│   └── oauth/
│       ├── discovery.py     → /.well-known endpoints
│       ├── endpoints.py     → /authorize /token /register
│       └── grants.py        → PKCE grant + scope helpers
├── tools/               # Pure Python utilities
│   ├── formatters.py        → format output for LLM
│   ├── smart_fields.py      → intelligent field selection
│   └── uri_schema.py        → parse odoo:// URI
├── wizard/
│   ├── model_picker.py      → adv.module.picker (module selection wizard)
│   └── model_picker_views.xml
├── data/
│   ├── server_config.xml    → singleton adv_server_config_default
│   └── oauth_cron.xml       → ir.cron GC OAuth
├── security/
│   ├── security.xml         → group_adv_admin, group_adv_user
│   └── ir.model.access.csv  → ACL for all models
└── views/               # Odoo UI
    ├── access_control_views.xml  → adv.module.access list/form
    ├── consent_templates.xml     → OAuth layout + error (no consent form)
    ├── menu.xml
    └── settings_views.xml
```

---

## 2. MCP Request Processing Flow

```
AI Client (Claude, Cursor…)
    │
    │  POST /mcp_server
    │  Authorization: Bearer <token>
    ▼
[1] AdvHttpGateway.dispatch()          ← server/dispatcher.py
    │  parse JSON body
    │  check Origin header (CORS)
    │  check MCP-Protocol-Version
    ▼
[2] ir.http._auth_method_adv_gateway() ← models/http_layer.py
    │  extract token from Authorization header
    │  try API key (scope adv / rpc)
    │  try OAuth access token
    │  → _bind_user(uid, su=False)     ← su=False is mandatory
    ▼
[3] AdvMCPHandler.process()            ← server/handler.py
    │  check gateway enabled
    │  parse JSON-RPC envelope (method, params, id)
    │  check rate limit (per-user: max_requests_for_uid)
    │  look up _ROUTE_TABLE → call handler method
    ▼
[4] Specific handler method
    │  initialize → _initialize()
    │  tools/list → _enumerate_tools()
    │  tools/call → _invoke_tool()
    │  resources/read → _fetch_resource()
    ▼
[5] _invoke_tool() → adv.tool.mixin method   ← models/tool_mixin.py
    │  check write scope (OAuth)
    │  check required args
    │  call tool method (search / get / add / attach / pipeline…)
    ▼
[6] Tool method (read_tools / write_tools)
    │  _check_op(model, operation)     ← check adv gate + Odoo ACL
    │  _resolve_model(model)           ← returns env(su=False)[model]
    │  ORM call (search / create / write / unlink)
    ▼
[7] Odoo ORM → PostgreSQL
    ▼
[8] adv.event.record_access()         ← write audit log
    ▼
Response → AI Client
```

---

## 3. Models

### 3.1 `adv.server.config` — Gateway Configuration Singleton

**File:** `models/config.py`

**Singleton pattern:** Only one record ever exists, pre-created via XML data (`data/server_config.xml`). `create()` is overridden to block additional records. `unlink()` is blocked unless the context has `force_unlink=True`.

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | Boolean | False | Master switch — disables all of `/mcp_server` |
| `enable_oauth` | Boolean | True | Enable OAuth 2.1 authorization server |
| `enable_rate_limiting` | Boolean | False | Enable request rate limiting |
| `request_limit` | Integer | 300 | Max requests / minute / user (regular users) |
| `admin_request_limit` | Integer | 0 | Max requests / minute for admin users (0 = use `request_limit`) |
| `enable_logging` | Boolean | True | Write adv.event entries |
| `log_retention_days` | Integer | 30 | Days to retain events (0 = keep forever) |
| `default_limit` | Integer | 10 | Default number of records returned |
| `max_limit` | Integer | 100 | Maximum allowed records per request |
| `max_smart_fields` | Integer | 15 | Maximum fields in smart field selection |
| `max_related_items` | Integer | 3 | Maximum related records when follow_relations is used |
| `allowed_origins` | Char | — | Comma-separated allowed Origins (CORS) |

**Key functions:**

| Function | Description |
|---|---|
| `_get_config()` | `env.ref("advanced_mcp_server.adv_server_config_default")` — retrieve singleton |
| `allowed_origin_set()` | Parse `allowed_origins` → lowercase `tuple`, strip trailing `/` |
| `write(vals)` | Override: calls `registry.clear_cache()` after each save to invalidate `@ormcache` |

---

### 3.2 `adv.module.access` — Module-Based Permission Layer

**File:** `models/module_access.py`

Instead of granting permissions per model, admins register **Odoo modules** (e.g. `sale`, `purchase`). All models whose `ir.model.modules` contains that module name are automatically exposed.

| Field | Type | Description |
|---|---|---|
| `module_name` | Char | Odoo module technical name, e.g. `sale` |
| `active` | Boolean | Quick enable/disable without deleting |
| `allow_read` | Boolean | Allow reading all models in the module |
| `allow_create` | Boolean | Allow creating records |
| `allow_write` | Boolean | Allow updating records |
| `allow_unlink` | Boolean | Allow deleting records |
| `allow_method_calls` | Boolean | Allow calling business methods via `run` |
| `notes` | Text | Admin notes |
| `model_ids` | Many2many (computed) | All `ir.model` records belonging to this module |
| `model_access_ids` | One2many → `adv.model.access` | Per-model permission overrides (auto-populated on create) |

**Module → Model mapping:**

`ir.model` has a `modules` Char field — a comma-separated list of module names (e.g. `"sale,sale_management"`). When checking a `model_name`, the code splits this field to find which modules define the model, then looks up those module names in `adv.module.access`.

**OR semantics for multi-module models:** A model can belong to multiple Odoo modules via `_inherit`. All matching active `adv.module.access` records are collected, and permissions are unioned: if **any** registered module allows an operation, the operation is allowed.

**Per-model overrides (`adv.model.access`):**

When a module access record is created, `_sync_model_access_ids()` auto-creates one `adv.model.access` row for every model in the module (all permission flags default to `True` = inherit module). When `module_id` changes, the rows are re-synced (old removed, new added). After a module upgrade that adds models, the "Sync Models" button triggers the same sync manually.

Effective permission = `module_perm AND model_override`. An override with all `True` is identical to no override — it only matters when an admin unchecks a flag to restrict that model further. The module-level permission is the absolute ceiling: if `allow_create=False` on the module, no model override can grant create.

**Key functions (all `@ormcache`):**

| Function | Description |
|---|---|
| `_module_names_for_model(model_name)` | Returns the list of module names from `ir.model.modules` for the given model |
| `_find_enabled_modules(model_name)` | Returns ALL active `adv.module.access` records covering model_name — supports OR semantics |
| `_get_model_override(module_rec, model_ir)` | Returns the `adv.model.access` override for a specific module+model pair, or empty recordset |
| `_model_ir(model_name)` | Looks up the `ir.model` record for model_name |
| `is_model_enabled(model_name)` | Is the model enabled? (cached) |
| `check_model_operation_enabled(model_name, operation)` | Is this specific operation allowed? Checks module perm AND model override (cached) |
| `is_method_call_enabled(model_name)` | Is method calling enabled? Checks module perm AND model override (cached) |
| `_sync_model_access_ids()` | Adds missing model rows and removes stale ones; called on create and when module_id changes |
| `action_sync_models()` | Button handler: calls `_sync_model_access_ids()` for manual re-sync after module upgrade |
| `_compute_model_ids` | Compute the many2many field: all `ir.model` records in the module |

**Wizard `adv.module.picker`** (`wizard/model_picker.py`): Dialog that lets admins select multiple installed modules at once and create corresponding `adv.module.access` records. Triggered by the "Add Modules" button in the list view.

---

### 3.3 `adv.event` — Structured Event Log

**File:** `models/audit_log.py`

| Field | Type | Description |
|---|---|---|
| `event_kind` | Selection | Event type: `auth_success/auth_failure/data_access/mutation/error/permission_denied/quota_exceeded/resource_fetch` |
| `event_class` | Selection | Group: `security/data/ops` (computed from `event_kind`) |
| `risk_score` | Integer (0–10) | Risk level (computed): `auth_failure=8`, `permission_denied=6`, `quota_exceeded=4` |
| `user_id` | Many2one → `res.users` | User who made the call |
| `ip_address` | Char | Client IP address |
| `session_id` | Char | `Mcp-Session-Id` header — groups multiple calls in one AI session |
| `auth_method` | Selection | `api_key / oauth / session` |
| `oauth_client_id` | Many2one → `adv.oauth.client` | OAuth client (if applicable) |
| `oauth_scope` | Char | Granted scope |
| `endpoint` | Char | `/mcp_server` |
| `resource_name` | Char | Odoo model name |
| `operation` | Char | `read/create/write/unlink/method` |
| `capability_name` | Char | Tool name: `search`, `get`, `add`, `attach`, `pipeline`… |
| `record_ids` | Char | IDs of affected records |
| `request_data` | Text | Parameter summary (clipped to 10000 chars) |
| `response_data` | Text | Result summary |
| `error_message` | Text | Error details if applicable |
| `duration_ms` | Integer | Processing time in milliseconds |

**Key functions:**

| Function | Description |
|---|---|
| `record_event(event_kind, **kw)` | Core function — checks `enable_logging`, creates entry in a savepoint |
| `record_access(model_name, operation, …)` | Shortcut for `data_access` |
| `record_error(error_message, …)` | Shortcut for `error` |
| `record_access_denied(model_name, operation, …)` | Shortcut for `permission_denied` |
| `record_quota_exceeded(user_id, …)` | Shortcut for `quota_exceeded` |
| `purge_old_entries(days)` | Deletes in batches of 1000, reads retention from config |

**Note:** `record_event` uses `savepoint()` so it does not roll back with the main transaction on failure; skips if cursor is `readonly`.

---

### 3.4 `adv.custom.tool` — Admin-Defined Custom Tools

**File:** `models/custom_tool.py`

| Field | Type | Description |
|---|---|---|
| `name` | Char | Tool name (must match `^[A-Za-z0-9_-]{1,64}$`, no conflict with builtins) |
| `description` | Text | Description for the LLM (critical — LLM reads this to decide when to call it) |
| `action_id` | Many2one → `ir.actions.server` | Python Code server action |
| `input_schema` | Text | JSON Schema declaring parameters (type must be `object`) |
| `is_readonly` | Boolean | Declares read-only hint — only callable by OAuth sessions with `adv:read` scope |
| `active` | Boolean | Show/hide in tools/list |

**Key functions:**

| Function | Description |
|---|---|
| `_user_can_run()` | Checks whether user is in `action_id.group_ids`; fallback: has write on the action model |
| `_run_tool(arguments)` | Injects `arguments` into `context['adv_tool_call']['args']`, runs server action, reads `result` |
| `_visible_tools()` | Filters active tools the current user can run |
| `_check_name()` | Constraint: no conflict with builtin tools, correct regex |
| `_check_action_is_code()` | Constraint: action must be Python Code (not a window action) |

**How server actions read/write results:**
```python
# Inside the Python Code server action:
args = env.context['adv_tool_call']['args']  # dict of parameters from LLM
env.context['adv_tool_call']['result'] = {"message": "Done"}  # return value to LLM
```

---

### 3.5 `adv.oauth.client` — Registered OAuth 2.1 Client

**File:** `models/oauth_client.py`
**Inherits:** `ClientMixin` (Authlib)

| Field | Type | Description |
|---|---|---|
| `client_id` | Char | Public identifier, unique |
| `client_name` | Char | Display name |
| `redirect_uris` | Text | Allowed URIs, one per line |
| `grant_types` | Char | `authorization_code refresh_token` |
| `response_types` | Char | `code` |
| `token_endpoint_auth_method` | Char | `none` (PKCE, no client secret required) |
| `scope` | Char | Registered scope |
| `created_via` | Char | `dcr` (dynamic registration) or blank |
| `active` | Boolean | Deactivating revokes all tokens for this client |

---

### 3.6 `adv.oauth.token` — Access Token & Refresh Token

**File:** `models/oauth_token.py`
**Inherits:** `TokenMixin` (Authlib)

> **Security:** Raw tokens are **never stored** — only their SHA-256 hash is stored.

| Field | Type | Description |
|---|---|---|
| `access_token_hash` | Char | SHA-256 of the access token |
| `refresh_token_hash` | Char | SHA-256 of the refresh token |
| `client` | Many2one → `adv.oauth.client` | Issuing client |
| `user_id` | Many2one → `res.users` | User the token was granted for |
| `scope` | Char | `adv:read` or `adv:write` or empty (full) |
| `audience` | Char | Resource URL (RFC 8707 binding) |
| `access_expires_at` | Datetime | Expires after 1 hour |
| `refresh_expires_at` | Datetime | Expires after 30 days |
| `revoked` | Boolean | Token has been revoked |
| `refresh_family_id` | Char | Refresh token family ID — on detected reuse, entire family is revoked |

**Key functions:**

| Function | Description |
|---|---|
| `_save_token(token, oauth2_request)` | Authlib callback — saves new token after issuance |
| `_get_valid_access_token(access_token)` | Find valid token by hash |
| `_get_valid_refresh_token(refresh_token)` | Find with `FOR UPDATE` (prevent race condition) |
| `_revoke() / _revoke_family(family_id)` | Revoke token / entire family |
| `_detect_refresh_reuse(refresh_token)` | Detect refresh token reuse → revoke family |
| `_gc_oauth()` | Garbage collect: delete expired codes, expired/revoked tokens, unused DCR clients |

---

## 4. Server Layer (not Odoo models)

### 4.1 `server/handler.py` — AdvMCPHandler

Main controller handling `POST /mcp_server`.

**`_ROUTE_TABLE`** — dict mapping MCP method → handler method name:
```python
_ROUTE_TABLE = {
    "initialize": "_initialize",
    "ping": "_ping",
    "tools/list": "_enumerate_tools",
    "tools/call": "_invoke_tool",
    "resources/templates/list": "_list_resource_templates",
    "resources/list": "_list_resources",
    "resources/read": "_fetch_resource",
}
```

**Decorator `@_handles(mcp_method)`:** registers a method into `_ROUTE_TABLE`.

**Flow inside `_invoke_tool(args)`:**
1. Find tool in builtin index → fallback to `adv.custom.tool`
2. Check write scope (OAuth `adv:read` blocks tools with `readOnlyHint=False`)
3. Validate required args against `input_schema`
4. Run tool inside `savepoint()`
5. `finally`: write `adv.event` (success or error)

**`_write_permitted()`:** `True` if no OAuth scope or scope contains write.

**Rate limiting:** `_apply_flow_control` uses `rate_limiter.max_requests_for_uid(uid)` — admins can be granted a higher limit via `admin_request_limit` in config.

---

### 4.2 `server/dispatcher.py` — AdvHttpGateway

Custom Odoo `Dispatcher` (routing_type = `adv_gateway`).

**`pre_dispatch(rule, args)`:**
- Checks `Origin` header against `allowed_origins` from config — returns 403 if no match
- Checks `MCP-Protocol-Version` header — returns 400 if version is unsupported
- Sets CORS response headers
- Handles preflight OPTIONS → 204

---

### 4.3 `server/auth_resolver.py` — Authentication

**`get_user_from_api_key(token, allowed_scopes, log_failure)`:**
- Tries `res.users.apikeys._check_credentials(scope=scope, key=token)` for each scope in `allowed_scopes`
- Returns a `res.users` record or `None`

---

### 4.4 `server/audit_writer.py` — Write Events Outside Transaction

**`push_event(uid, write_log, failure_message)`:**
- Opens a new independent cursor (`registry.cursor()`)
- Calls `write_log(env["adv.event"])` — a lambda passed in
- Used when log must be written before the main transaction commits (e.g. auth failure)

---

### 4.5 `server/rate_limiter.py` — RequestThrottle

Sliding window counter, in-memory, per-worker process.

**`RequestThrottle(window_seconds)`:**
- `is_limited(key, max_count)` → True if threshold exceeded within the window
- `key` is typically `(dbname, uid)` (per-user) or `(dbname, ip_address)` (per-IP)

**Module-level functions:**

| Function | Description |
|---|---|
| `throttling_active()` | Reads `enable_rate_limiting` from config |
| `max_requests_per_window()` | Reads `request_limit` from config — default limit for all users |
| `max_requests_for_uid(uid)` | If user is admin and `admin_request_limit > 0` → returns higher limit; otherwise returns `request_limit` |

---

### 4.6 `server/protocol.py` — JSON-RPC Helpers

| Function | Description |
|---|---|
| `parse_envelope(data)` | Reads `method`, `params`, `id` from JSON-RPC body |
| `wrap_ok(result, ref)` | Builds response `{"jsonrpc":"2.0","id":ref,"result":result}` |
| `wrap_err(error_type, detail, ref, hint)` | Builds error response with standard codes |

---

## 5. Tool Layer

### 5.1 `models/tool_mixin.py` — AdvToolMixin (abstract)

Base class for all tools. All modules extend via `_inherit = "adv.tool.mixin"`.

**`@adv_tool(name, description, input_schema, operation, **annotations)`:**
Decorator that attaches metadata to a method: `method._adv_tool = {...}`.

**`_get_adv_tools()`** (`@ormcache`):
Scans MRO to find all methods with `_adv_tool` → builds dict `{name: {method_name, description, input_schema, operation, annotations}}`.

**`_resolve_model(model)`:**
```python
return self.env(su=False)[model]  # force su=False — ORM always enforces ACL
```

**`_check_op(model, operation)`:**
1. Checks `adv.module.access` — is the operation enabled?
2. Calls `self.env(su=False)[model].browse().check_access(operation)` — enforces `ir.model.access` + `ir.rule`

---

### 5.2 Read Tools — 9 data read tools

**File:** `models/read_tools.py`

| Tool | Python Method | Description |
|---|---|---|
| `search` | `search(model, domain, spec, fields, limit, offset, order)` | Search with Odoo domain or simple `spec` dict; auto-unescapes HTML-encoded operators |
| `get` | `fetch(model, record_id, fields, depth)` | Fetch a single record; `depth` 0–2 expands Many2one fields |
| `describe` | `describe(model, field_names)` | No `model`: list all resources. With `model`: return full schema |
| `resources` | `resources()` | List `odoo://` URI templates for binary fields and attachments |
| `aggregate` | `aggregate(model, row_groupby, col_groupby, aggregates, domain, limit, offset)` | Flat groupby or two-way cross-tab |
| `me` | `me()` | User, timezone, company, permitted models, OAuth scope, pending activities, unread messages |
| `count` | `count(model, domain)` | Count records matching a domain — faster than `search` when only a total is needed |
| `explain` | `explain(model, record_id)` | History, workflow state, and attachments of a single record |
| `compare` | `compare(model, record_id_a, record_id_b, fields)` | Compare two records, returns list of differing fields |

> **Note:** The tool name is what the AI client calls via `tools/call`. The Python method name may differ (e.g. the `get` tool uses the internal method `fetch` to avoid conflict with Odoo ORM).

**`_whoami_activity_context()`** (helper for `me`):
- Reads `mail.activity` to count pending activities for the user (deadline ≤ today)
- Reads `mail.notification` to count unread messages
- Wrapped in `try/except` — gracefully skips if the `mail` module is not installed

**`_unescape_domain(domain)`:** LLMs sometimes HTML-encode operators (`&lt;`, `&gt;`, `&amp;`) — this function unescapes them before passing to the ORM.

---

### 5.3 Write Tools — 6 data write tools

**File:** `models/write_tools.py`

| Tool | Python Method | Description |
|---|---|---|
| `add` | `add(model, values, dry_run)` | Create a record; `dry_run=True` previews without saving |
| `edit` | `edit(model, record_id, values)` | Update specified fields on a record |
| `drop` | `drop(model, record_id)` | Delete a record; returns `affected_relations` |
| `run` | `run(model, method, record_ids, args, kwargs)` | Call a business method (requires `allow_method_calls`); includes `message_post` |
| `attach` | `attach(filename, content, mimetype, res_model, res_id)` | Upload a file (base64) to Odoo, creates `ir.attachment`, returns `odoo://attachment/{id}` |
| `pipeline` | `pipeline(operations)` | Run multiple write operations in one savepoint; rolls back all on any error |

**`attach` — details:**
- `content` is a base64-encoded string
- Gate: `ir.attachment` create enabled in `adv.module.access` **OR** `res_model` create enabled
- Returns URI `odoo://attachment/{id}` for use with `resources/read`

**`pipeline` — details:**
- Allowed tools inside pipeline: `add`, `edit`, `drop`, `run`
- Maximum `_BATCH_MAX_OPS = 20` operations per pipeline
- Template syntax: `{{op_id.field}}` in any arg string → resolves from the `structuredContent` of a previous operation
  - Full match `"{{order.id}}"` → returns the original value (int/bool/…) without string casting
  - Partial match `"prefix-{{order.name}}"` → concatenated as a string
- `_extract_ref` looks for the field in `sc["record"]` first, falls back to `sc` directly → supports both `add`/`edit` (which have `record`) and `drop`/`run` (which do not)

---

## 6. OAuth 2.1 Flow — Auto-Approve

> **No consent form.** When a user is already logged in to Odoo, GET `/authorize` automatically approves and redirects back to the client without showing a confirmation screen.

```
Client (Claude Desktop, Cursor)
    │
    │ GET /.well-known/oauth-protected-resource   → discovery.py
    │ GET /.well-known/oauth-authorization-server → discovery.py
    │
    │ POST /mcp_server/oauth/register             → endpoints.py
    │ (dynamic client registration)               → adv.oauth.client._register_client()
    │
    │ GET /mcp_server/oauth/authorize?
    │      client_id=...&code_challenge=...&scope=adv:write
    │      (user is already logged in to Odoo)
    │
    │ endpoints.py auto-approve:
    │   granted = "adv:write" if client has write + requested write scope
    │   granted = "adv:read"  otherwise
    │   → create_authorization_response(grant_user=current_user)
    │
    │ Redirect → redirect_uri?code=<auth_code>
    │ (code hash stored in adv.oauth.code)
    │
    │ POST /mcp_server/oauth/token
    │      code=...&code_verifier=...
    │ → adv.oauth.token._save_token()
    │ → returns access_token (1 hour) + refresh_token (30 days)
    │
    │ POST /mcp_server
    │      Authorization: Bearer <access_token>
    │ → _resolve_token() hashes and looks up → adv.oauth.token._get_valid_access_token()
```

**POST path is preserved:** Clients can send POST with `action=deny` for an explicit deny. Only GET is auto-approved.

**Rotating refresh token:** Each refresh token exchange generates a new token and revokes the old one. If an old token is reused (reuse attack), the entire `refresh_family_id` is revoked.

---

## 7. Security Groups

Defined in `security/security.xml`:

| Group | XML ID | Privileges |
|---|---|---|
| `group_adv_admin` | `advanced_mcp_server.group_adv_admin` | Full config access, view events, manage OAuth, higher rate limit |
| `group_adv_user` | `advanced_mcp_server.group_adv_user` | See Adv MCP menu, use MCP endpoint |

`group_adv_admin` implies `group_adv_user`.

---

## 8. Two-Layer Permission Model

Every tool call passes through **2 independent permission checks**:

```
Request
  │
  ▼ Layer 1: adv.module.access
  │  Find which modules define model_name (via ir.model.modules)
  │  Is that module in active adv.module.access?  → No → AccessError
  │  Is the operation enabled on the module?      → No → AccessError
  │  (OR semantics: any matching module allowing the operation is sufficient)
  │
  ▼ Layer 2: Odoo native ACL (env.su=False enforced)
  │  ir.model.access.check()  → No → AccessError (Odoo native)
  │  ir.rule domain match?    → No → AccessError (Odoo native)
  │
  ▼ ORM call (search/create/write/unlink)
  │  env.su = False → ORM calls check_access() again internally
  │
  ▼ PostgreSQL
```

**Why is `su=False` important?** Odoo's custom routing type creates an env with `su=True` in some cases. With `su=True`, `ir.model.access.check()` returns `True` immediately without checking groups. Both `_bind_user()` and `_resolve_model()` force `su=False` to ensure Odoo ACL is always enforced.

---

## 9. Cron Jobs

**`data/oauth_cron.xml`** defines an `ir.cron` that periodically runs `adv.oauth.token._gc_oauth()`:
- Deletes expired `adv.oauth.code` records
- Deletes expired / revoked `adv.oauth.token` records
- Deletes `adv.oauth.client` records created via DCR that are no longer in use (> 30 days)

`adv.event.purge_old_entries()` is called according to `log_retention_days` in config.

---

## 10. Utility Modules

| File | Description |
|---|---|
| `tools/formatters.py` | Format Odoo records into LLM-friendly text/JSON |
| `tools/smart_fields.py` | Automatically select important fields when none are specified (skips binary and non-stored computed fields) |
| `tools/uri_schema.py` | Parse `odoo://record/{model}/{id}/{field}` and `odoo://attachment/{id}` |
| `server/sanitizer.py` | Sanitize exception messages — prevents traceback/SQL leaking to the client |
| `models/hash_utils.py` | `sha256_hex(s)` — used for all token/code hashing |

---

## 11. Tool Return Format

Every tool returns a standard dict:

```python
{
    "content": [{"type": "text", "text": "...human-readable summary..."}],
    "structuredContent": {
        # Tool-specific data (optional)
        "record": {"id": 42, "display_name": "..."},   # add / edit
        "records": [...],                               # search
        "count": 10,                                    # count / aggregate
        "results": [...],                               # pipeline
        "attachment_id": 7, "uri": "odoo://attachment/7",  # attach
        "diffs": {...},                                 # compare
    }
}
```

`_result(text, structured)` in `read_tools.py` — factory function used by all tools.
