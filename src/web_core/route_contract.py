def _normalise_path_prefix(prefix: str) -> str:
    stripped_prefix = prefix.strip()
    if stripped_prefix in {"", "/"}:
        raise ValueError("Route prefixes must not be empty or root-mounted.")

    return f"/{stripped_prefix.strip('/')}"


API_PATH_PREFIX = _normalise_path_prefix("/api")
PARTIAL_PATH_PREFIX = _normalise_path_prefix("/partials")
