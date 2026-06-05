from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import FormData

from auth_ext.options import IdentityOptions
from auth_ext.sessions import (
    authenticate_user,
    clear_session_cookie,
    complete_authentication_ceremony,
    create_local_user_from_signup,
    destroy_session_token,
    request_password_reset,
    request_verification,
    reset_password,
    resolve_current_user,
    set_session_cookie,
    verify_user,
)
from web_core.route_contract import API_PATH_PREFIX
from web_core.routing import HtmlRouteDefinition, HtmlView, ModuleRoutes


@dataclass(frozen=True, slots=True)
class IdentityRouteSet:
    page_routes: tuple[HtmlRouteDefinition, ...]
    api_router: APIRouter


async def current_user_state(request: Request) -> dict[str, object]:
    user = await resolve_current_user(request)
    if user is None:
        return {"authenticated": False}

    return {
        "authenticated": True,
        "email": user.email,
        # Keep this optional state endpoint out of authorisation decisions.
        "is_verified": user.is_verified,
    }


def _identity_context(request: Request, **extra: Any) -> dict[str, Any]:
    del request
    return dict(extra)


def _form_value(form_data: FormData, name: str, default: str = "") -> str:
    value = form_data.get(name, default)
    return value if isinstance(value, str) else default


async def _request_form_data(request: Request) -> FormData:
    return await request.form()


def _identity_options(request: Request) -> IdentityOptions:
    options = getattr(request.app.state, "identity_options", None)
    if not isinstance(options, IdentityOptions):
        raise RuntimeError("Identity options are not configured on the application.")

    return options


def _public_signup_enabled(request: Request) -> bool:
    return _identity_options(request).account_creation_policy == "public-signup"


def normalise_return_to(value: str | None, default: str = "/account") -> str:
    candidate = (value or "").strip()
    if (
        not candidate.startswith("/")
        or candidate.startswith("//")
        or "\\" in candidate
        or "\r" in candidate
        or "\n" in candidate
    ):
        return default

    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return default

    return candidate


@dataclass(frozen=True, slots=True)
class LoginPageView(HtmlView):
    template_name: str = "identity/pages/login.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        context = _identity_context(
            request,
            page_title="Sign in",
            public_signup_enabled=_public_signup_enabled(request),
            return_to=normalise_return_to(request.query_params.get("return_to")),
        )
        return renderer.render_page(self.template_name, request, context)


@dataclass(frozen=True, slots=True)
class LoginSubmitView(HtmlView):
    template_name: str = "identity/pages/login.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        form_data = await _request_form_data(request)
        email = _form_value(form_data, "email").strip()
        password = _form_value(form_data, "password")
        return_to = normalise_return_to(_form_value(form_data, "return_to"))

        user = await authenticate_user(request, email, password)
        if user is None:
            context = _identity_context(
                request,
                page_title="Sign in",
                public_signup_enabled=_public_signup_enabled(request),
                email=email,
                return_to=return_to,
                form_error="Email or password is incorrect.",
            )
            return renderer.render_page(
                self.template_name,
                request,
                context,
                status_code=401,
            )

        ceremony_result = await complete_authentication_ceremony(request, user)
        if ceremony_result.is_failure() or ceremony_result.value is None:
            context = _identity_context(
                request,
                page_title="Sign in",
                public_signup_enabled=_public_signup_enabled(request),
                email=email,
                return_to=return_to,
                form_error="Email or password is incorrect.",
            )
            return renderer.render_page(
                self.template_name,
                request,
                context,
                status_code=401,
            )

        response = RedirectResponse(url=return_to, status_code=303)
        set_session_cookie(
            response,
            request,
            ceremony_result.value,
            _identity_options(request),
        )
        return response


@dataclass(frozen=True, slots=True)
class LogoutSubmitView(HtmlView):
    async def render(self, request: Request, renderer: Any) -> Response:
        await destroy_session_token(request)
        response = RedirectResponse(url="/", status_code=303)
        clear_session_cookie(response, request, _identity_options(request))
        return response


@dataclass(frozen=True, slots=True)
class SignupPageView(HtmlView):
    template_name: str = "identity/pages/signup.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        if not _public_signup_enabled(request):
            raise HTTPException(status_code=404)

        context = _identity_context(request, page_title="Create account")
        return renderer.render_page(self.template_name, request, context)


@dataclass(frozen=True, slots=True)
class SignupSubmitView(HtmlView):
    template_name: str = "identity/pages/signup.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        if not _public_signup_enabled(request):
            raise HTTPException(status_code=404)

        form_data = await _request_form_data(request)
        email = _form_value(form_data, "email").strip()
        password = _form_value(form_data, "password")
        creation_result = await create_local_user_from_signup(
            request,
            email,
            password,
        )
        created = creation_result.is_ok()
        context = _identity_context(
            request,
            page_title="Create account",
            email=email,
            form_message=("Account created. You can now sign in." if created else None),
            form_error=(
                None if created else "Unable to create account with those details."
            ),
        )
        return renderer.render_page(
            self.template_name,
            request,
            context,
            status_code=201 if created else 400,
        )


@dataclass(frozen=True, slots=True)
class LogoutPageView(HtmlView):
    template_name: str = "identity/pages/logout.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        context = _identity_context(request, page_title="Sign out")
        return renderer.render_page(self.template_name, request, context)


@dataclass(frozen=True, slots=True)
class AccountPageView(HtmlView):
    template_name: str = "identity/pages/account.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        context = _identity_context(
            request,
            page_title="Account",
        )
        return renderer.render_page(self.template_name, request, context)


@dataclass(frozen=True, slots=True)
class PasswordResetPageView(HtmlView):
    template_name: str = "identity/pages/password_reset.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        context = _identity_context(request, page_title="Reset password")
        return renderer.render_page(
            self.template_name,
            request,
            context,
        )


@dataclass(frozen=True, slots=True)
class PasswordResetRequestView(HtmlView):
    template_name: str = "identity/pages/password_reset.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        form_data = await _request_form_data(request)
        email = _form_value(form_data, "email").strip()
        await request_password_reset(request, email)
        context = _identity_context(
            request,
            page_title="Reset password",
            email=email,
            form_message="If the account exists, a reset link has been queued.",
        )
        return renderer.render_page(
            self.template_name,
            request,
            context,
        )


@dataclass(frozen=True, slots=True)
class PasswordResetConfirmView(HtmlView):
    template_name: str = "identity/pages/password_reset.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        form_data = await _request_form_data(request)
        token = _form_value(form_data, "token")
        password = _form_value(form_data, "password")
        did_reset = await reset_password(request, token, password)
        context = _identity_context(
            request,
            page_title="Reset password",
            form_message="Password reset complete." if did_reset else None,
            form_error=None if did_reset else "The reset token is invalid or expired.",
        )
        return renderer.render_page(
            self.template_name,
            request,
            context,
            status_code=200 if did_reset else 400,
        )


@dataclass(frozen=True, slots=True)
class VerificationPageView(HtmlView):
    template_name: str = "identity/pages/verify.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        context = _identity_context(request, page_title="Verify email")
        return renderer.render_page(self.template_name, request, context)


@dataclass(frozen=True, slots=True)
class VerificationRequestView(HtmlView):
    template_name: str = "identity/pages/verify.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        form_data = await _request_form_data(request)
        email = _form_value(form_data, "email").strip()
        await request_verification(request, email)
        context = _identity_context(
            request,
            page_title="Verify email",
            email=email,
            form_message=(
                "If the account can be verified, a verification link has been queued."
            ),
        )
        return renderer.render_page(self.template_name, request, context)


@dataclass(frozen=True, slots=True)
class VerificationConfirmView(HtmlView):
    template_name: str = "identity/pages/verify.html"

    async def render(self, request: Request, renderer: Any) -> Response:
        form_data = await _request_form_data(request)
        token = _form_value(form_data, "token")
        verification_result = await verify_user(request, token)
        did_verify = verification_result.is_ok()
        context = _identity_context(
            request,
            page_title="Verify email",
            form_message="Email verification complete." if did_verify else None,
            form_error=(
                None if did_verify else "The verification token is invalid or expired."
            ),
        )
        return renderer.render_page(
            self.template_name,
            request,
            context,
            status_code=200 if did_verify else 400,
        )


def build_identity_route_set(
    options: IdentityOptions | None = None,
) -> IdentityRouteSet:
    del options
    normalised_api_prefix = API_PATH_PREFIX.rstrip("/")
    api_router = APIRouter(prefix=f"{normalised_api_prefix}/identity")
    api_router.add_api_route(
        "/current-user",
        current_user_state,
        methods=["GET"],
        include_in_schema=False,
        name="identity:api:current-user",
    )

    page_routes = [
        HtmlRouteDefinition(
            path="/login",
            name="identity:login",
            methods=("GET",),
            surface="page",
            view=LoginPageView(),
        ),
        HtmlRouteDefinition(
            path="/login",
            name="identity:login-submit",
            methods=("POST",),
            surface="page",
            view=LoginSubmitView(),
        ),
    ]
    page_routes.extend(
        [
            HtmlRouteDefinition(
                path="/signup",
                name="identity:signup",
                methods=("GET",),
                surface="page",
                view=SignupPageView(),
            ),
            HtmlRouteDefinition(
                path="/signup",
                name="identity:signup-submit",
                methods=("POST",),
                surface="page",
                view=SignupSubmitView(),
            ),
            HtmlRouteDefinition(
                path="/logout",
                name="identity:logout-page",
                methods=("GET",),
                surface="page",
                view=LogoutPageView(),
            ),
            HtmlRouteDefinition(
                path="/logout",
                name="identity:logout",
                methods=("POST",),
                surface="page",
                view=LogoutSubmitView(),
            ),
            HtmlRouteDefinition(
                path="/account",
                name="identity:account",
                methods=("GET",),
                surface="page",
                view=AccountPageView(),
            ),
            HtmlRouteDefinition(
                path="/password/reset",
                name="identity:password-reset",
                methods=("GET",),
                surface="page",
                view=PasswordResetPageView(),
            ),
            HtmlRouteDefinition(
                path="/password/reset",
                name="identity:password-reset-request",
                methods=("POST",),
                surface="page",
                view=PasswordResetRequestView(),
            ),
            HtmlRouteDefinition(
                path="/password/reset/confirm",
                name="identity:password-reset-confirm",
                methods=("POST",),
                surface="page",
                view=PasswordResetConfirmView(),
            ),
            HtmlRouteDefinition(
                path="/verify",
                name="identity:verify",
                methods=("GET",),
                surface="page",
                view=VerificationPageView(),
            ),
            HtmlRouteDefinition(
                path="/verify",
                name="identity:verify-request",
                methods=("POST",),
                surface="page",
                view=VerificationRequestView(),
            ),
            HtmlRouteDefinition(
                path="/verify/confirm",
                name="identity:verify-confirm",
                methods=("POST",),
                surface="page",
                view=VerificationConfirmView(),
            ),
        ]
    )

    return IdentityRouteSet(
        page_routes=tuple(page_routes),
        api_router=api_router,
    )


def build_identity_module_routes(
    options: IdentityOptions | None = None,
) -> ModuleRoutes:
    route_set = build_identity_route_set(options)
    return ModuleRoutes(
        page_routes=route_set.page_routes,
        api_routers=(route_set.api_router,),
    )


module_routes = build_identity_module_routes()
