{
    "name": "Magenest Odoo MCP Server",
    "description": """
        Magenest Odoo MCP Server is a free Model Context Protocol integration that connects MCP-compatible AI assistants and agents with Odoo. It enables users to retrieve and work with live Odoo records through natural-language interactions while maintaining the access controls and permissions configured in Odoo.
    """,
    "version": "19.0.3.0.0",
    "summary": "Expose Odoo to AI assistants via Model Context Protocol",
    "author": "Magenest",
    "website": "https://www.magenest.com",
    "category": "Magenest AI",
    "depends": ["base", "base_setup", "mail", "rpc", "web"],
    "external_dependencies": {
        "python": ["authlib>=1.6.12,<1.7.0", "defusedxml", "packaging"],
    },
    "data": [
        "security/security.xml",
        "security/ir.model.access.csv",
        "data/server_config.xml",
        "data/oauth_cron.xml",
        "wizard/module_picker_views.xml",
        "wizard/bulk_action_views.xml",
        "views/access_control_views.xml",
        "views/audit_log_views.xml",
        "views/oauth_views.xml",
        "views/custom_tool_views.xml",
        "views/settings_views.xml",
        "views/apikeys_views.xml",
        "views/consent_templates.xml",
        "views/menu.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "mgn_mcp_server/static/src/security_banner.js",
            "mgn_mcp_server/static/src/security_banner.xml",
            "mgn_mcp_server/static/src/module_tags_field.js",
        ],
    },
    "installable": True,
    "application": True,
    "license": "OPL-1",
}
