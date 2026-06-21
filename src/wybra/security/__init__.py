"""Public web-security policy API."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "COOP_HEADER_NAME": "wybra.security.headers",
    "COOP_STATE_ATTRIBUTE": "wybra.security.headers",
    "CorsPolicy": "wybra.security.cors",
    "CorsPolicySet": "wybra.security.cors",
    "CrossOriginOpenerPolicy": "wybra.security.headers",
    "DefaultSecurityCapability": "wybra.security.capabilities",
    "SECURITY_MIDDLEWARE_STATE_ATTRIBUTE": "wybra.security.headers",
    "SECURITY_OPTIONS_STATE_ATTRIBUTE": "wybra.security.headers",
    "SecurityCapability": "wybra.security.capabilities",
    "SecurityHeaderOptions": "wybra.security.headers",
    "SecuritySettings": "wybra.security.settings",
    "cross_origin_opener_policy": "wybra.security.headers",
    "load_cors_policy": "wybra.security.cors",
    "load_cors_policy_set": "wybra.security.cors",
    "module_config": "wybra.security.config",
    "normalise_url_path_prefix": "wybra.security.cors",
    "post_setup_site": "wybra.security.capabilities",
    "register_security_headers": "wybra.security.headers",
    "render_nginx_cors_config": "wybra.security.nginx",
    "security_provider_configured": "wybra.security.capabilities",
    "setup_site": "wybra.security.capabilities",
    "validate_security": "wybra.security.validation",
}

provides_security_capability = True


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'wybra.security' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "COOP_HEADER_NAME",
    "COOP_STATE_ATTRIBUTE",
    "CorsPolicy",
    "CorsPolicySet",
    "CrossOriginOpenerPolicy",
    "DefaultSecurityCapability",
    "SECURITY_MIDDLEWARE_STATE_ATTRIBUTE",
    "SECURITY_OPTIONS_STATE_ATTRIBUTE",
    "SecurityCapability",
    "SecurityHeaderOptions",
    "SecuritySettings",
    "cross_origin_opener_policy",
    "load_cors_policy",
    "load_cors_policy_set",
    "module_config",
    "normalise_url_path_prefix",
    "post_setup_site",
    "provides_security_capability",
    "register_security_headers",
    "render_nginx_cors_config",
    "security_provider_configured",
    "setup_site",
    "validate_security",
]
