import logging
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy.exc import SQLAlchemyError

from wevra.auth.sessions import mark_session_cookie_for_clearing, resolve_current_user
from wevra.web.context import TemplateContext, add_to_context

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TemplateUser:
    id: str
    email: str
    is_active: bool
    is_verified: bool
    is_superuser: bool


async def identity_template_context(
    request: Request,
    context: TemplateContext,
) -> TemplateContext:
    try:
        user = await resolve_current_user(request)
    except SQLAlchemyError as exc:
        logger.warning(
            "Auth session lookup failed while building template context; "
            "treating request as anonymous and clearing the session cookie.",
            extra={
                "request_path": getattr(getattr(request, "url", None), "path", None),
                "error_type": type(exc).__name__,
                "auth_context": "template_context",
            },
            exc_info=True,
        )
        mark_session_cookie_for_clearing(request)
        user = None

    if user is None:
        return context.with_values(
            user=None,
            identity={"authenticated": False},
        )

    return context.with_values(
        user=TemplateUser(
            id=str(user.id),
            email=user.email,
            is_active=user.is_active,
            is_verified=user.is_verified,
            is_superuser=user.is_superuser,
        ),
        identity={
            "authenticated": True,
            "is_verified": user.is_verified,
            "is_superuser": user.is_superuser,
        },
    )


add_to_context(identity_template_context)
