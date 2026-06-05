"""Reusable FastAPI/Starlette web composition infrastructure.

`web_core` owns web resource, routing, context, CSRF, theme, and validation
contracts; it must not import host application packages, `auth_ext`, settings,
or startup code.
"""

__all__ = [
    "composition",
    "conventions",
    "context",
    "csrf",
    "diagnostics",
    "dispatcher",
    "errors",
    "form_security",
    "resources",
    "route_contract",
    "routes",
    "routing",
    "style_contract",
    "surfaces",
    "static",
    "templates",
    "theme",
    "validation",
    "views",
]
