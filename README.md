# Magenest Odoo MCP Server (`mgn_mcp_server`)

An Odoo 19 module that exposes a live Odoo instance to AI assistants and agents through the **Model Context Protocol (MCP)**. It lets MCP-compatible clients read and write Odoo records using natural-language-driven tool calls, while enforcing the same access controls and permissions already configured in Odoo.

- **Repository:** [MagenestJSC/advanced_mcp_server](https://github.com/MagenestJSC/advanced_mcp_server)
- **Odoo version:** 19.0
- **License:** OPL-1
- **Author:** [Magenest](https://www.magenest.com)

## Overview

The module adds a JSON-RPC 2.0 gateway (`POST /mcp_server`) to Odoo that speaks a small, uniform set of "tools" instead of exposing raw ORM/XML-RPC calls. Every request runs as a real Odoo user, so record rules, field-level security, and module access defined in Odoo are respected automatically. On top of that baseline the module adds a dedicated admin-configurable access-control layer, OAuth 2.1 for browser-based clients, rate limiting, and an audit log of every call.

## Key Features

- **Unified tool surface** — a small set of generic tools (`describe`, `get`, `search`, `aggregate`, `me`, `count`, `explain`, `compare`, `resources` for reads; `add`, `edit`, `drop`, `run`, `attach`, `pipeline` for writes) cover arbitrary Odoo models instead of one endpoint per model.
- **Fine-grained access control** — per-module (`adv.module.access`) and per-model (`adv.model.access`) overrides decide which models and operations (read/write/delete/method calls) are reachable through the gateway, independent of a user's normal Odoo permissions.
- **Custom tools** — admins can expose their own `ir.actions.server` actions as first-class MCP tools (`adv.custom.tool`), with JSON Schema input validation.
- **Two authentication modes**
  - **API keys** — scoped keys minted from the standard Odoo API key wizard.
  - **OAuth 2.1** — a built-in authorization server (`server/oauth/`) supporting the standard grant types, PKCE, dynamic client registration, and a discovery endpoint, for browser-based and third-party clients.
- **Safety & abuse controls** — a master enable/disable switch, per-user and per-admin sliding-window rate limiting (`server/rate_limiter.py`), request size caps, CORS origin allow-listing, and sanitized error responses that never leak internal tracebacks (`server/sanitizer.py`).
- **Audit logging** — every call is recorded to `adv.event.log` (actor, resource, operation, request/response payloads, errors) with configurable retention, independent of the calling transaction so a failed request is still logged.
- **LLM-friendly payloads** — smart field selection (`tools/smart_fields.py`) and hierarchical text formatters (`tools/formatters.py`) trim and shape Odoo's `read`/`fields_get` output for token-efficient consumption, plus an `odoo://` URI scheme (`tools/uri_schema.py`) for referencing binary fields and attachments as MCP resources.
- **Legacy XML-RPC proxy** — `server/rpc_proxy.py` bridges XML-RPC clients through the same gateway and audit/rate-limit pipeline.

## Architecture

```
mgn_mcp_server/
├── models/         # Access control, OAuth entities, custom tools, audit log, tool_mixin (adv_tool registry)
├── server/         # HTTP gateway, JSON-RPC dispatcher/protocol, auth, rate limiting, sanitizer, audit writer
│   └── oauth/      # OAuth 2.1 authorization server (grants, endpoints, discovery)
├── tools/          # Field selection, output formatting, odoo:// URI helpers
├── views/          # Backend UI for configuration, access control, audit log, API keys, OAuth clients
├── wizard/         # Module picker / bulk action wizards
├── security/       # Access rights and record rules
└── data/           # Default gateway configuration, OAuth maintenance cron
```

Read and write capabilities are implemented as plain Python methods tagged with `@adv_tool` on `adv.tool.mixin` (see `models/read_tools.py` and `models/write_tools.py`); any module that inherits this mixin can contribute additional tools, which are discovered automatically via MRO scan — no central registry to edit.

### Available tools

| Tool | Type | Purpose |
|---|---|---|
| `describe` | read | List accessible resources, or return the full schema for one model |
| `get` | read | Fetch a single record by ID, with optional relation expansion (`depth`) |
| `search` | read | Search records via an Odoo domain or a simplified key-value `spec` |
| `aggregate` | read | Group/pivot records by one or two dimensions |
| `me` | read | Current session identity: user, timezone, company, permitted resources, active OAuth scope |
| `count` | read | Count records matching a domain, without fetching them |
| `explain` | read | Contextual summary of a record: key fields, recent chatter, state, attachments |
| `compare` | read | Diff two records of the same model, field by field |
| `resources` | read | List `odoo://` resource URIs for binary fields/attachments |
| `add` | write | Create a record (supports `dry_run` validation) |
| `edit` | write | Update specific fields on an existing record |
| `drop` | write | Delete a record (reports records that would be affected by cascade) |
| `run` | write | Call a public model method (including `message_post` for chatter) |
| `attach` | write | Upload a file as an `ir.attachment`, returning an `odoo://` URI |
| `pipeline` | write | Run multiple `add`/`edit`/`drop`/`run` operations atomically, with result chaining |

## Requirements

- Odoo 19.0
- Python dependencies: `authlib>=1.6.12,<1.7.0`, `defusedxml`, `packaging`
- Odoo module dependencies: `base`, `base_setup`, `mail`, `rpc`, `web`

## Installation

1. Copy `mgn_mcp_server` into your Odoo `addons` path.
2. Install the Python dependencies:
   ```bash
   pip install "authlib>=1.6.12,<1.7.0" defusedxml packaging
   ```
3. Update the apps list and install **Magenest Odoo MCP Server** from the Odoo Apps menu.
4. Go to the module's settings to enable the gateway, configure rate limiting, and set up access control for the models you want to expose.

## Configuration

All gateway settings live on the singleton `adv.server.config` record, editable from the module's Settings screen:

| Setting | Default | Description |
|---|---|---|
| Gateway Enabled | `False` | Master switch for the `/mcp_server` endpoint |
| OAuth 2.1 | `True` | Enables the built-in OAuth 2.1 authorization flow |
| Rate Limiting | `False` | Enables per-user request throttling |
| Requests / Minute (per user) | `300` | Per-user rate cap when rate limiting is on |
| Admin Requests / Minute | `0` (= same as regular) | Higher cap for gateway admins |
| Event Logging | `True` | Enables audit logging to `adv.event.log` |
| Log Retention (days) | `30` | `0` = keep forever |
| Default Record Limit | `10` | Default page size for `search`/`aggregate` |
| Max Record Limit | `100` | Hard cap on page size |
| Max Smart Fields | `15` | Max fields auto-selected per record in LLM-friendly output |
| Max Related Items | `3` | Max related records auto-fetched when expanding relations |
| Allowed Origins | *(empty = unrestricted)* | Comma-separated list of allowed browser Origins for CORS |

Model-level access is granted via **Adv MCP Module Access** / **Adv MCP Per-Model Permission Override** records, which decide which models and operations are reachable independent of a user's normal Odoo group permissions.

## Endpoint

Once enabled, the gateway is reachable at:

```
POST /mcp_server
POST /mcp_server/rpc
```

using JSON-RPC 2.0 request/response envelopes, authenticated either with an Odoo API key or an OAuth 2.1 bearer token obtained through the module's built-in authorization server.

## Security Notes

- The gateway is **disabled by default** — it must be explicitly turned on in settings.
- All access still passes through Odoo's own record rules and field security in addition to the module's access-control layer.
- Internal errors are sanitized before being returned to clients; only recognized Odoo exceptions (`UserError`, `AccessError`, `ValidationError`, `MissingError`) surface their message, everything else returns a generic error.
- `run` (method calls) is blocked for ORM internals and private (underscore-prefixed) methods, and must be explicitly allowed per model.
