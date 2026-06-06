FORM_CONTENT_TYPES = frozenset(
    {
        "application/x-www-form-urlencoded",
        "multipart/form-data",
    }
)
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def normalise_content_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def is_form_content_type(content_type: str) -> bool:
    return normalise_content_type(content_type) in FORM_CONTENT_TYPES


def is_safe_method(method: str) -> bool:
    return method.upper() in SAFE_METHODS
