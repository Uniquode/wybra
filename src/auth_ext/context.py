from dataclasses import dataclass

from fastapi import Request

from auth_ext.sessions import resolve_current_user
from web_core.context import add_to_context


@dataclass(frozen=True, slots=True)
class TemplateUser:
    id: str
    email: str
    is_active: bool
    is_verified: bool
    is_superuser: bool


async def identity_template_context(request: Request) -> dict[str, object]:
    user = await resolve_current_user(request)
    if user is None:
        return {
            "user": None,
            "identity": {"authenticated": False},
        }

    return {
        "user": TemplateUser(
            id=str(user.id),
            email=user.email,
            is_active=user.is_active,
            is_verified=user.is_verified,
            is_superuser=user.is_superuser,
        ),
        "identity": {
            "authenticated": True,
            "is_verified": user.is_verified,
            "is_superuser": user.is_superuser,
        },
    }


add_to_context(identity_template_context)
