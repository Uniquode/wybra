"""Public form and CSRF capability API."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORT_MODULES = {
    "CSRF_COOKIE_NAME": "wybra.forms.csrf",
    "CSRF_EXEMPT_ENDPOINT_ATTR": "wybra.forms.csrf",
    "CSRF_FIELD_NAME": "wybra.forms.csrf",
    "CSRF_FORM_DATA_STATE_ATTR": "wybra.forms.csrf",
    "CSRF_HEADER_NAME": "wybra.forms.csrf",
    "CSRF_RESPONSE_FINALISATION_STATE_ATTR": "wybra.forms.csrf",
    "CsrfProtector": "wybra.forms.csrf",
    "DefaultFormsCapability": "wybra.forms.capabilities",
    "FORMS_CONFIG_SECTION": "wybra.forms.config",
    "FormsCapability": "wybra.forms.capabilities",
    "FormsSettings": "wybra.forms.settings",
    "GENERATE_LOCAL_CSRF_SECRET": "wybra.forms.config",
    "csrf_exempt": "wybra.forms.csrf",
    "csrf_response_finalisation_requested": "wybra.forms.csrf",
    "forms_provider_configured": "wybra.forms.capabilities",
    "is_form_content_type": "wybra.forms.security",
    "is_safe_method": "wybra.forms.security",
    "module_config": "wybra.forms.config",
    "normalise_content_type": "wybra.forms.security",
    "request_csrf_response_finalisation": "wybra.forms.csrf",
    "request_form_data": "wybra.forms.csrf",
    "setup_site": "wybra.forms.setup",
    "validate_csrf": "wybra.forms.csrf",
    "validate_forms": "wybra.forms.validation",
    "validation_targets": "wybra.forms.validation",
}

provides_forms_capability = True


def __getattr__(name: str) -> Any:
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module 'wybra.forms' has no attribute {name!r}")
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


__all__ = [
    "CSRF_COOKIE_NAME",
    "CSRF_EXEMPT_ENDPOINT_ATTR",
    "CSRF_FIELD_NAME",
    "CSRF_FORM_DATA_STATE_ATTR",
    "CSRF_HEADER_NAME",
    "CSRF_RESPONSE_FINALISATION_STATE_ATTR",
    "CsrfProtector",
    "DefaultFormsCapability",
    "FORMS_CONFIG_SECTION",
    "FormsCapability",
    "FormsSettings",
    "GENERATE_LOCAL_CSRF_SECRET",
    "csrf_exempt",
    "csrf_response_finalisation_requested",
    "forms_provider_configured",
    "is_form_content_type",
    "is_safe_method",
    "module_config",
    "normalise_content_type",
    "provides_forms_capability",
    "request_csrf_response_finalisation",
    "request_form_data",
    "setup_site",
    "validate_csrf",
    "validate_forms",
    "validation_targets",
]
