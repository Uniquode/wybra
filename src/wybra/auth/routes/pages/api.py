from fastapi import Request

from wybra.auth.sessions import resolve_current_user

from .shared import api_router


async def current_user_state(request: Request) -> dict[str, object]:
    user = await resolve_current_user(request)
    if user is None:
        return {"authenticated": False}

    return {
        "authenticated": True,
        "email": user.email,
        "is_verified": user.is_verified,
    }


@api_router.get(
    "/current-user",
    include_in_schema=False,
    name="auth:api:current-user",
)
async def current_user_api(request: Request) -> dict[str, object]:
    return await current_user_state(request)
