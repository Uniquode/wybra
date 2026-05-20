# ruff: noqa: B018
# Vulture whitelist: framework entry points and future integration hooks.


class _Whitelist:
    pass


_ = _Whitelist()

_.health  # FastAPI route handler registered through APIRouter
_.init_database  # FastAPI lifespan integration hook, wired when DB startup is enabled
_.close_database  # FastAPI lifespan integration hook, wired when DB shutdown is enabled
