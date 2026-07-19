FORM_CONTENT_TYPES = frozenset(
    {
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    }
)
FORM_BODY_MAX_BYTES = 1_048_576
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def normalise_content_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def is_form_content_type(content_type: str) -> bool:
    return normalise_content_type(content_type) in FORM_CONTENT_TYPES


def is_safe_method(method: str) -> bool:
    return method.upper() in SAFE_METHODS


__all__ = (
    "FORM_CONTENT_TYPES",
    "FORM_BODY_MAX_BYTES",
    "SAFE_METHODS",
    "is_form_content_type",
    "is_safe_method",
    "normalise_content_type",
)
