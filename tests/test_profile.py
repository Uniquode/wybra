from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from wybra.auth import AuthCapability, login_required  # noqa: F401
from wybra.config import ConfigService, ConfigSourceError, MappingConfigSource
from wybra.core import InputValidationError
from wybra.core.resources import PackageResourceSource
from wybra.db import DatabaseCapability, SqlAlchemyDatabaseCapability
from wybra.db.models import metadata
from wybra.db.persistence import create_database
from wybra.forms import (
    CsrfProtector,
    DefaultFormsCapability,
    FormsCapability,
    forms_rendering_context,
)
from wybra.media import (
    FilesystemMediaCapability,
    MediaCapability,
    MediaCapabilityError,
    MediaSettings,
)
from wybra.profile import (
    DEFAULT_EDITABLE_PROFILE_FIELDS,
    PROFILE_FIELD_METADATA,
    ProfileCapability,
    ProfileCapabilityError,
    ProfileInputError,
    ProfileSettings,
    SiteProfileCapability,
    country_choices,
    normalise_phone_contact,
    profile_picture_storage_key,
    render_profile_bio,
    subdivision_choices,
)
from wybra.profile.forms import ProfileEditForm
from wybra.profile.models import UserPhoneContact, UserProfile
from wybra.profile.routes import profile_router
from wybra.profile.validation import validate_profile
from wybra.site import Site, SiteCapabilityError, start
from wybra.template import DefaultTemplateCapability, TemplateCapability
from wybra.widgets.config import WidgetsSettings
from wybra.widgets.login import login_widget_state
from wybra.widgets.navigation import (
    DropdownPanel,
    KeyboardShortcut,
    NavigationItem,
    NavigationMenu,
)

_CREATED_SITES: list[Site] = []


def _resource_text(package: str, resource_path: str) -> str:
    return resources.files(package).joinpath(resource_path).read_text(encoding="utf-8")


def _css_declaration_exists(css: str, property_name: str, value: str) -> bool:
    return (
        re.search(
            rf"{re.escape(property_name)}\s*:\s*{re.escape(value)}\s*;",
            css,
        )
        is not None
    )


@dataclass(frozen=True, slots=True)
class ProfileUser:
    id: uuid.UUID
    email: str


class ProfileTemplateStub:
    def render_template(self, template_name: str, context: dict[str, object]) -> str:
        return self._render(template_name, context)

    def render_page(
        self,
        request: Request,
        template_name: str,
        context: dict[str, object],
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        del request
        return HTMLResponse(
            self._render(template_name, context),
            status_code=status_code,
        )

    def _render(self, template_name: str, context: dict[str, object]) -> str:
        profile_form = context.get("profile_form") or context["form"]
        preferred_name = ""
        field_errors = ""
        phone_states = ""
        if isinstance(profile_form, ProfileEditForm):
            preferred_name = str(profile_form.fields["preferred_name"].value or "")
            field_errors = ",".join(
                f"{field}:{'|'.join(errors)}"
                for field, errors in profile_form.errors.items()
                if field is not None
            )
            phone_subdivision = profile_form.fields.get("phone_subdivision_code")
            phone_number = profile_form.fields.get("phone_number")
            if phone_subdivision is not None:
                phone_states += "|subdivisions=" + ",".join(
                    option.label for option in phone_subdivision.options()
                )
            if phone_number is not None and phone_number.disabled:
                phone_states += "|phone_disabled"
            if phone_number is not None:
                phone_states += f"|phone_number={phone_number.value or ''}"
            country = profile_form.fields.get("phone_country_code")
            if country is not None:
                phone_states += f"|country={country.value or ''}"
            if phone_subdivision is not None:
                phone_states += f"|subdivision={phone_subdivision.value or ''}"
        phone_status = context.get("phone_contact_status")
        if phone_status:
            phone_states += f"|status={phone_status}"
        return (
            f"{template_name}|preferred_name={preferred_name}|"
            f"csrf_field={context.get('csrf_field_name', '')}|"
            f"phone_prefix={context.get('phone_prefix', '')}|"
            f"phone_states={phone_states}|field_errors={field_errors}"
        )

    def render_partial(
        self,
        request: Request,
        template_name: str,
        context: dict[str, object],
        *,
        status_code: int = 200,
    ) -> HTMLResponse:
        return self.render_page(
            request,
            template_name,
            context,
            status_code=status_code,
        )


class AuthCapabilityStub:
    settings = None
    fastapi_users = None

    def __init__(self, user: ProfileUser | None) -> None:
        self._user = user

    @property
    def optional_current_user(self):
        async def current_user(_request: object) -> ProfileUser | None:
            return self._user

        return current_user

    @property
    def login_required(self):
        async def current_user(_request: object) -> ProfileUser:
            if self._user is None:
                raise RuntimeError("missing test user")
            return self._user

        return current_user

    @property
    def anonymous_required(self):
        async def anonymous(_request: object) -> None:
            return None

        return anonymous


def _site_with_database(tmp_path: Path) -> Site:
    site = Site(
        app=FastAPI(),
        config=ConfigService(
            [MappingConfigSource({"app": {"modules": ()}})],
            discover_module_config=False,
        ),
    )
    database = create_database(f"sqlite+aiosqlite:///{tmp_path / 'profile.sqlite3'}")
    site.provide_capability(
        DatabaseCapability,
        SqlAlchemyDatabaseCapability.from_connections({"default": database}),
    )
    _CREATED_SITES.append(site)
    return site


def _profile_route_site(
    tmp_path: Path,
    user: ProfileUser,
    *,
    profile_config: dict[str, object] | None = None,
) -> Site:
    app = FastAPI()
    app.include_router(profile_router)

    async def current_user() -> ProfileUser:
        return user

    app.dependency_overrides[login_required] = current_user
    app.state.csrf = CsrfProtector("test-secret")
    config_data: dict[str, object] = {
        "app": {"modules": ("wybra.profile", "wybra.forms")}
    }
    if profile_config is not None:
        config_data["wybra.profile"] = profile_config
    site = Site(
        app=app,
        config=ConfigService([MappingConfigSource(config_data)]),
    )
    database = create_database(
        f"sqlite+aiosqlite:///{tmp_path / 'profile-route.sqlite3'}"
    )
    site.provide_capability(
        DatabaseCapability,
        SqlAlchemyDatabaseCapability.from_connections({"default": database}),
    )
    site.provide_capability(
        ProfileCapability,
        SiteProfileCapability(site.capability_proxy(MediaCapability)),
    )
    site.require_capability(ProfileCapability).media.finalise_optional()
    site.provide_capability(FormsCapability, DefaultFormsCapability(app.state.csrf))
    site.provide_capability(TemplateCapability, ProfileTemplateStub())
    app.state.site = site
    _CREATED_SITES.append(site)
    return site


def _widget_site(user: ProfileUser, *, profile_route: bool = True) -> Site:
    app = FastAPI()

    async def endpoint() -> dict[str, bool]:
        return {"ok": True}

    app.add_api_route("/login", endpoint, name="auth:login")
    app.add_api_route("/logout", endpoint, name="auth:logout")
    app.add_api_route("/account", endpoint, name="auth:account")
    if profile_route:
        app.add_api_route("/profile", endpoint, name="profile:edit")
    site = Site(
        app=app,
        config=ConfigService(
            [MappingConfigSource({"app": {"modules": ("wybra.widgets",)}})]
        ),
    )
    site.provide_capability(AuthCapability, AuthCapabilityStub(user))
    site.provide_capability(
        ProfileCapability,
        SiteProfileCapability(site.capability_proxy(MediaCapability)),
    )
    app.state.site = site
    app.state.widgets_settings = WidgetsSettings()
    _CREATED_SITES.append(site)
    return site


async def _create_site_schema(site: Site) -> None:
    async with site.require_capability(DatabaseCapability).transaction() as db_session:

        def _create_all(sync_session) -> None:
            metadata.create_all(sync_session.get_bind())

        await db_session.run_sync(_create_all)


@pytest.fixture(autouse=True)
def close_created_sites():
    yield
    while _CREATED_SITES:
        asyncio.run(_CREATED_SITES.pop().close())


def test_profile_metadata_exposes_profile_table() -> None:
    table = metadata.tables["profile_user_profile"]

    assert table.c.user_id.foreign_keys
    assert table.c.profile_picture_media_id.nullable is True
    assert table.c.preferred_name.nullable is True
    assert table.c.display_name.nullable is True
    assert table.c.bio.nullable is True
    assert table.c.first_name.nullable is True
    assert table.c.last_name.nullable is True
    assert table.c.pronouns.nullable is True
    assert table.c.phone_number.nullable is True
    assert table.c.website_links.nullable is True
    assert table.c.country_region.nullable is True
    assert table.c.city.nullable is True
    assert table.c.postal_code.nullable is True
    assert table.c.job_title.nullable is True
    assert table.c.company.nullable is True
    assert table.c.company_industry.nullable is True
    assert table.c.department.nullable is True
    assert table.c.date_time_format.nullable is True
    assert table.c.theme.nullable is True
    assert table.c.notification_preferences.nullable is True
    assert table.c.profile_visibility.nullable is False
    assert table.c.marketing_consent.nullable is False
    assert table.c.terms_accepted_at.nullable is True
    assert table.c.data_deletion_requested.nullable is False


def test_profile_metadata_exposes_phone_contact_table() -> None:
    table = metadata.tables["profile_phone_contact"]

    assert table.c.user_id.foreign_keys
    assert table.c.country_code.nullable is False
    assert table.c.subdivision_code.nullable is True
    assert table.c.normalised_number.nullable is False
    assert table.c.number_type.nullable is False
    assert table.c.sms_capable.nullable is False
    assert table.c.verified_at.nullable is True


def test_validate_profile_accepts_configured_profile_module() -> None:
    class Settings:
        modules = ("wybra.profile",)

    result = validate_profile(Settings())

    assert result.is_ok is True


def test_validate_profile_reports_absent_profile_module() -> None:
    class Settings:
        modules = ()

    result = validate_profile(Settings())

    assert result.is_ok is False
    assert result.errors == (
        "wybra.profile must be configured to validate profile resources.",
    )


def test_profile_settings_enable_editing_with_default_editable_fields() -> None:
    settings = ProfileSettings.load_settings({})

    assert settings.editing_enabled is True
    assert settings.editable_fields == DEFAULT_EDITABLE_PROFILE_FIELDS
    assert "profile_picture" not in settings.editable_fields


def test_profile_settings_reads_configured_editable_fields() -> None:
    settings = ProfileSettings.load_settings(
        {"editable_fields": "preferred_name,display_name,bio"}
    )

    assert settings.editable_fields == ("preferred_name", "display_name", "bio")


def test_profile_settings_rejects_unknown_editable_field() -> None:
    with pytest.raises(ConfigSourceError, match="unknown editable profile field"):
        config = ConfigService(
            [
                MappingConfigSource(
                    {
                        "app": {"modules": ("wybra.profile",)},
                        "wybra.profile": {
                            "editable_fields": ("preferred_name", "email")
                        },
                    }
                )
            ]
        )
        ProfileSettings.load_settings(config)


def test_profile_field_metadata_describes_default_text_fields() -> None:
    assert PROFILE_FIELD_METADATA["preferred_name"].label == "Preferred name"
    assert PROFILE_FIELD_METADATA["display_name"].label == "Display name"
    assert PROFILE_FIELD_METADATA["pronouns"].kind == "pronouns"
    assert PROFILE_FIELD_METADATA["profile_links"].kind == "links"
    assert PROFILE_FIELD_METADATA["bio"].max_length == 1024


def test_profile_settings_reads_configured_pronoun_options() -> None:
    settings = ProfileSettings.load_settings(
        {"pronoun_options": ("ze|zir", ("fae", "faer"))}
    )

    assert tuple(option.value for option in settings.pronoun_options) == (
        "ze|zir",
        "fae|faer",
    )


def test_normalise_phone_contact_uses_country_context_for_local_numbers() -> None:
    contact = normalise_phone_contact("0412 345 678", country_code="AU")

    assert contact.country_code == "AU"
    assert contact.normalised_number == "+61412345678"
    assert contact.number_type == "mobile"
    assert contact.sms_capable is True


def test_normalise_phone_contact_accepts_e164_numbers() -> None:
    contact = normalise_phone_contact("+14155552671", country_code="US")

    assert contact.country_code == "US"
    assert contact.normalised_number == "+14155552671"


def test_normalise_phone_contact_requires_country_for_local_numbers() -> None:
    with pytest.raises(ProfileInputError, match="country"):
        normalise_phone_contact("0412 345 678", country_code=None)


def test_normalise_phone_contact_keeps_subdivision_out_of_normalisation() -> None:
    with_subdivision = normalise_phone_contact(
        "0412 345 678",
        country_code="AU",
        subdivision_code="AU-VIC",
    )
    without_subdivision = normalise_phone_contact(
        "0412 345 678",
        country_code="AU",
    )

    assert with_subdivision.normalised_number == without_subdivision.normalised_number
    assert with_subdivision.subdivision_code == "AU-VIC"


def test_country_choices_are_iso_backed_with_dial_prefixes_and_flags() -> None:
    countries = {country.code: country for country in country_choices()}

    assert countries["AU"].name == "Australia"
    assert countries["AU"].dial_prefix == "+61"
    assert len(countries["AU"].flag) == 2


def test_subdivision_choices_are_iso_backed() -> None:
    subdivisions = {choice.code: choice for choice in subdivision_choices("AU")}

    assert subdivisions["AU-VIC"].name == "Victoria"


@pytest.mark.anyio
async def test_profile_setup_registers_profile_capability_before_media_exists() -> None:
    site = await start(
        FastAPI(),
        config_source=MappingConfigSource(
            {
                "app": {
                    "modules": (
                        "wybra.profile",
                        "wybra.media",
                        "wybra.forms",
                        "wybra.auth",
                        "wybra.db",
                    ),
                    "database_url": "sqlite+aiosqlite:///profile.sqlite3",
                },
            }
        ),
    )

    assert site.has_capability(ProfileCapability) is True
    assert site.has_capability(MediaCapability) is True
    assert site.has_capability(AuthCapability) is True
    assert site.has_capability(DatabaseCapability) is True


@pytest.mark.anyio
async def test_profile_edit_route_renders_without_creating_profile(
    tmp_path: Path,
) -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _profile_route_site(tmp_path, user)
    await _create_site_schema(site)

    response = TestClient(site.app).get("/profile")

    assert response.status_code == 200
    assert "profile/pages/edit.html" in response.text
    assert "preferred_name=" in response.text
    async with site.require_capability(DatabaseCapability).session() as session:
        assert (
            await site.require_capability(ProfileCapability).get_profile(
                session,
                user.id,
            )
            is None
        )


@pytest.mark.anyio
async def test_profile_edit_route_renders_existing_profile(
    tmp_path: Path,
) -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _profile_route_site(tmp_path, user)
    await _create_site_schema(site)
    async with site.require_capability(DatabaseCapability).transaction() as session:
        session.add(UserProfile(user_id=user.id, preferred_name="David"))

    response = TestClient(site.app).get("/profile")

    assert response.status_code == 200
    assert "preferred_name=David" in response.text


@pytest.mark.anyio
async def test_profile_edit_route_populates_phone_contact_and_status(
    tmp_path: Path,
) -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _profile_route_site(tmp_path, user)
    await _create_site_schema(site)
    async with site.require_capability(DatabaseCapability).transaction() as session:
        session.add_all(
            [
                UserPhoneContact(
                    user_id=user.id,
                    country_code="AU",
                    normalised_number="+61412345678",
                    number_type="mobile",
                    sms_capable=True,
                    verified_at=1234.0,
                ),
            ]
        )

    response = TestClient(site.app).get("/profile")

    assert response.status_code == 200
    assert "country=AU" in response.text
    assert "phone_number=+61412345678" in response.text
    assert "status=Verified" in response.text


@pytest.mark.anyio
async def test_profile_edit_route_post_creates_profile(
    tmp_path: Path,
) -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _profile_route_site(tmp_path, user)
    await _create_site_schema(site)
    nonce = "a" * 32
    token = site.app.state.csrf.create_token(nonce)
    client = TestClient(site.app)
    client.cookies.set(site.app.state.csrf.cookie_name, nonce)

    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "preferred_name": "David",
            "display_name": "David Nugent",
            "pronoun_pair": "they|their",
            "bio": "Profile text",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "http://testserver/profile"
    async with site.require_capability(DatabaseCapability).session() as session:
        profile = await site.require_capability(ProfileCapability).get_profile(
            session,
            user.id,
        )
    assert profile is not None
    assert profile.preferred_name == "David"
    assert profile.display_name == "David Nugent"
    assert profile.pronouns == {"direct": "they", "possessive": "their"}
    assert profile.bio == "Profile text"


@pytest.mark.anyio
async def test_profile_edit_route_valid_noop_returns_to_invoking_page(
    tmp_path: Path,
) -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _profile_route_site(tmp_path, user)
    await _create_site_schema(site)
    nonce = "a" * 32
    token = site.app.state.csrf.create_token(nonce)
    client = TestClient(site.app)
    client.cookies.set(site.app.state.csrf.cookie_name, nonce)

    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "return_to": "/account",
            "preferred_name": "",
            "display_name": "",
            "pronoun_pair": "",
            "profile_link_website": "",
            "bio": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/account"
    async with site.require_capability(DatabaseCapability).session() as session:
        assert (
            await site.require_capability(ProfileCapability).get_profile(
                session,
                user.id,
            )
            is None
        )


@pytest.mark.anyio
async def test_profile_edit_route_clears_existing_profile_fields(
    tmp_path: Path,
) -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _profile_route_site(tmp_path, user)
    await _create_site_schema(site)
    async with site.require_capability(DatabaseCapability).transaction() as session:
        session.add(
            UserProfile(
                user_id=user.id,
                preferred_name="David",
                display_name="David Nugent",
                bio="Existing bio",
                pronouns={"direct": "they", "possessive": "their"},
                website_links={"website": "https://example.test"},
            )
        )
    nonce = "a" * 32
    token = site.app.state.csrf.create_token(nonce)
    client = TestClient(site.app)
    client.cookies.set(site.app.state.csrf.cookie_name, nonce)

    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "preferred_name": "",
            "display_name": "",
            "pronoun_pair": "",
            "profile_link_website": "",
            "bio": "",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    async with site.require_capability(DatabaseCapability).session() as session:
        profile = await site.require_capability(ProfileCapability).get_profile(
            session,
            user.id,
        )
    assert profile is not None
    assert profile.preferred_name is None
    assert profile.display_name is None
    assert profile.bio is None
    assert profile.pronouns is None
    assert profile.website_links is None


@pytest.mark.anyio
async def test_profile_edit_route_re_renders_invalid_form(
    tmp_path: Path,
) -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _profile_route_site(tmp_path, user)
    await _create_site_schema(site)
    nonce = "a" * 32
    token = site.app.state.csrf.create_token(nonce)
    client = TestClient(site.app)
    client.cookies.set(site.app.state.csrf.cookie_name, nonce)

    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "preferred_name": "David",
            "profile_link_website": "javascript:alert(1)",
        },
    )

    assert response.status_code == 400
    assert "preferred_name=David" in response.text
    assert "profile_link_website:Profile link URL scheme" in response.text
    async with site.require_capability(DatabaseCapability).session() as session:
        assert (
            await site.require_capability(ProfileCapability).get_profile(
                session,
                user.id,
            )
            is None
        )


@pytest.mark.anyio
async def test_profile_edit_route_ignores_disabled_submitted_fields(
    tmp_path: Path,
) -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _profile_route_site(
        tmp_path,
        user,
        profile_config={"editable_fields": ("preferred_name",)},
    )
    await _create_site_schema(site)
    async with site.require_capability(DatabaseCapability).transaction() as session:
        session.add(
            UserProfile(
                user_id=user.id,
                preferred_name="Previous",
                bio="Existing bio",
            )
        )
    nonce = "a" * 32
    token = site.app.state.csrf.create_token(nonce)
    client = TestClient(site.app)
    client.cookies.set(site.app.state.csrf.cookie_name, nonce)

    response = client.post(
        "/profile",
        data={
            "csrf_token": token,
            "preferred_name": "David",
            "bio": "Submitted bio",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    async with site.require_capability(DatabaseCapability).session() as session:
        profile = await site.require_capability(ProfileCapability).get_profile(
            session,
            user.id,
        )
    assert profile is not None
    assert profile.preferred_name == "David"
    assert profile.bio == "Existing bio"


def test_profile_edit_template_renders_declarative_form_fields() -> None:
    templates = DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.template", directory="templates"),
            PackageResourceSource(package="wybra.forms", directory="templates"),
            PackageResourceSource(package="wybra.profile", directory="templates"),
        )
    )
    csrf = {"csrf_field_name": "csrf_token", "csrf_token": "token"}
    profile_form = ProfileEditForm(
        settings=ProfileSettings(),
        values={"preferred_name": "David", "phone_country_code": "AU"},
    )

    class ProfileUrlContext:
        def url_for(self, name: str) -> str:
            assert name.startswith("phone-contact-fields")
            return "/profile/phone-contact/fields"

    html = templates.render_template(
        "profile/pages/edit.html",
        {
            **csrf,
            **forms_rendering_context(templates, csrf, url_context=ProfileUrlContext()),
            "asset_url": lambda path: f"/static/{path}",
            "editable_fields": DEFAULT_EDITABLE_PROFILE_FIELDS,
            "form_error": None,
            "page_title": "Edit profile",
            "phone_contact_status": "Not verified",
            "phone_contacts": (),
            "profile_form": profile_form,
            "profile_settings": ProfileSettings(),
            "route_name": "profile:edit",
            "theme_attribute": None,
        },
    )

    assert 'class="wybra-form"' in html
    assert "styles/forms.css" in html
    assert "styles/profile.css" not in html
    assert 'method="post"' in html
    assert 'name="csrf_token"' in html
    assert 'name="preferred_name"' in html
    assert 'value="David"' in html
    assert 'name="phone_country_code"' in html
    assert ">Australia<" in html
    assert "🇦🇺 Australia +61" not in html
    assert 'hx-get="/profile/phone-contact/fields"' in html
    assert 'class="wybra-phone-contact-control"' in html
    assert 'id="phone_number_dial_prefix"' in html
    assert 'name="phone_subdivision_code"' in html
    assert not re.search(r'id="phone_number"[^>]*disabled', html)
    assert "🇦🇺 +61</span>" in html
    assert ">Not verified<" in html
    assert "Phone contacts" not in html
    assert "data-wybra-profile-form" in html
    assert "data-wybra-profile-save" in html
    assert ">Save Changes</button>" in html
    assert re.search(
        r"<button[^>]*data-wybra-profile-save[^>]*disabled",
        html,
    )
    assert "data-wybra-profile-cancel" in html
    assert ">Cancel</button>" in html
    assert re.search(
        r"<button[^>]*data-wybra-profile-cancel[^>]*disabled",
        html,
    )
    assert 'form.addEventListener("input", setActionState)' in html
    assert 'cancel.addEventListener("click"' in html
    assert "restoreInitialValues();" in html
    assert 'form.addEventListener("htmx:afterSwap"' in html
    assert 'querySelectorAll("[hx-get]")' in html


def test_profile_edit_template_suppresses_phone_status_when_phone_has_error() -> None:
    templates = DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.template", directory="templates"),
            PackageResourceSource(package="wybra.forms", directory="templates"),
            PackageResourceSource(package="wybra.profile", directory="templates"),
        )
    )
    csrf = {"csrf_field_name": "csrf_token", "csrf_token": "token"}
    profile_form = ProfileEditForm(
        settings=ProfileSettings(),
        values={"phone_country_code": "AU", "phone_number": "not-a-number"},
    )
    profile_form.parse({"phone_country_code": "AU", "phone_number": "not-a-number"})

    html = templates.render_template(
        "profile/pages/edit.html",
        {
            **csrf,
            **forms_rendering_context(templates, csrf),
            "asset_url": lambda path: f"/static/{path}",
            "editable_fields": DEFAULT_EDITABLE_PROFILE_FIELDS,
            "form_error": None,
            "page_title": "Edit profile",
            "phone_contact_status": "Not verified",
            "phone_contacts": (),
            "profile_form": profile_form,
            "profile_settings": ProfileSettings(),
            "route_name": "profile:edit",
            "theme_attribute": None,
        },
    )

    assert "Phone contact number is invalid." in html
    assert "Not verified" not in html


def test_profile_edit_form_uses_phone_contact_control_for_normalisation() -> None:
    form = ProfileEditForm(settings=ProfileSettings())

    result = form.parse(
        {
            "phone_country_code": "AU",
            "phone_subdivision_code": "AU-VIC",
            "phone_number": "0412 345 678",
        }
    )

    normalised = form.normalised_phone_contact()
    assert result.is_valid
    assert normalised is not None
    assert normalised.country_code == "AU"
    assert normalised.subdivision_code == "AU-VIC"
    assert normalised.normalised_number == "+61412345678"


@pytest.mark.anyio
async def test_profile_phone_fields_fragment_uses_selected_country(
    tmp_path: Path,
) -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _profile_route_site(tmp_path, user)
    await _create_site_schema(site)
    client = TestClient(site.app)

    response = client.get(
        "/profile/phone-contact/fields?"
        "phone_country_code=AU&"
        "phone_subdivision_code=AU-VIC&"
        "phone_number=%2B61412345678",
        headers={"HX-Request": "true"},
    )

    assert response.status_code == 200
    assert "forms/widgets/phone_contact_fields.html" in response.text
    assert "phone_prefix=🇦🇺 +61" in response.text
    assert "Victoria" in response.text
    assert "subdivision=AU-VIC" in response.text
    assert "phone_number=+61412345678" in response.text
    assert "phone_disabled" not in response.text


@pytest.mark.anyio
async def test_login_widget_state_builds_settings_menu() -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _widget_site(user)

    state = await login_widget_state(
        SimpleNamespace(app=site.app, url=SimpleNamespace(path="/account", query=""))
    )

    assert state is not None
    assert state.profile_path == "/profile?return_to=%2Faccount"
    assert state.settings_menu is not None
    assert state.settings_menu.label == "Settings"
    assert tuple(item.label for item in state.settings_menu.menu.items) == (
        "Account",
        "Login & Security",
        "Profile",
    )
    assert tuple(item.path for item in state.settings_menu.menu.items) == (
        "/account",
        "/account",
        "/profile?return_to=%2Faccount",
    )
    assert state.logout_path == "/logout"


@pytest.mark.anyio
async def test_login_widget_state_does_not_nest_profile_return_to() -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _widget_site(user)

    state = await login_widget_state(
        SimpleNamespace(
            app=site.app,
            url=SimpleNamespace(
                path="/profile",
                query="return_to=%2Faccount",
            ),
        )
    )

    assert state is not None
    assert state.profile_path == "/profile?return_to=%2Faccount"
    assert state.settings_menu is not None
    assert state.settings_menu.menu.items[-1].path == "/profile?return_to=%2Faccount"


@pytest.mark.anyio
async def test_login_widget_state_omits_profile_link_when_route_missing() -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _widget_site(user, profile_route=False)

    state = await login_widget_state(SimpleNamespace(app=site.app))

    assert state is not None
    assert state.profile_path is None
    assert state.settings_menu is not None
    assert tuple(item.label for item in state.settings_menu.menu.items) == (
        "Account",
        "Login & Security",
    )
    assert tuple(item.path for item in state.settings_menu.menu.items) == (
        "/account",
        "/account",
    )


@pytest.mark.anyio
async def test_login_widget_state_omits_profile_link_when_navigation_disabled() -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _widget_site(user)
    site.app.state.widgets_settings = WidgetsSettings(
        default_profile_avatar_navigation=False
    )

    state = await login_widget_state(SimpleNamespace(app=site.app))

    assert state is not None
    assert state.profile_path is None
    assert state.settings_menu is not None
    assert tuple(item.label for item in state.settings_menu.menu.items) == (
        "Account",
        "Login & Security",
    )


@pytest.mark.anyio
async def test_login_widget_state_omits_profile_link_when_settings_missing() -> None:
    user = ProfileUser(id=uuid.uuid4(), email="david@example.test")
    site = _widget_site(user)
    delattr(site.app.state, "widgets_settings")

    state = await login_widget_state(SimpleNamespace(app=site.app))

    assert state is not None
    assert state.profile_path is None
    assert state.settings_menu is not None
    assert tuple(item.label for item in state.settings_menu.menu.items) == (
        "Account",
        "Login & Security",
    )


def test_login_widget_template_renders_avatar_after_logout() -> None:
    templates = DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.widgets", directory="templates"),
        )
    )

    html = templates.render_template(
        "components/login_control.html",
        {
            "login_widget": SimpleNamespace(
                authenticated=True,
                login_path=None,
                logout_path="/logout",
                profile_image=SimpleNamespace(
                    src=None,
                    alt="Profile picture",
                    fallback_text="D",
                ),
                profile_path="/profile",
                settings_menu=DropdownPanel(
                    label="Settings",
                    menu=NavigationMenu(
                        label="Settings",
                        items=(
                            NavigationItem(
                                label="Account",
                                path="/account",
                            ),
                            NavigationItem(
                                label="Login & Security",
                                path="/account",
                            ),
                            NavigationItem(label="Profile", path="/profile"),
                        ),
                    ),
                ),
            ),
            "route_name": "home",
        },
    )

    settings_position = html.index('aria-label="Settings"')
    logout_position = html.index("Logout")
    avatar_position = html.index("login-widget__avatar")
    assert settings_position < logout_position < avatar_position
    assert '<span class="wybra-dropdown-panel' in html
    assert 'type="button"' in html
    assert "anchor-name: --wybra-dropdown-panel-trigger;" in html
    assert 'popovertarget="wybra-dropdown-panel"' in html
    assert "position-anchor: --wybra-dropdown-panel-trigger;" in html
    assert "popover" in html
    assert "Account" in html
    assert "Login &amp; Security" in html
    assert 'href="/profile"' in html


@pytest.mark.parametrize(
    (
        "csrf_context",
        "logout_path",
        "expects_logout_control",
        "expects_post_form",
    ),
    (
        (
            {"csrf_field_name": "csrf_token", "csrf_token": "secure-token"},
            "/logout",
            True,
            True,
        ),
        ({}, "/logout", True, False),
        ({"csrf_field_name": "csrf_token"}, "/logout", True, False),
        ({"csrf_token": "secure-token"}, "/logout", True, False),
        (
            {"csrf_field_name": "csrf_token", "csrf_token": "secure-token"},
            None,
            False,
            False,
        ),
        ({}, None, False, False),
    ),
)
def test_login_widget_template_renders_logout_control_for_context(
    csrf_context: dict[str, str],
    logout_path: str | None,
    expects_logout_control: bool,
    expects_post_form: bool,
) -> None:
    templates = DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.widgets", directory="templates"),
        )
    )
    context = {
        "login_widget": SimpleNamespace(
            authenticated=True,
            login_path=None,
            logout_path=logout_path,
            profile_image=None,
            profile_path=None,
            settings_menu=None,
        ),
        "route_name": "home",
    } | csrf_context

    html = templates.render_template(
        "components/login_control.html",
        context,
    )

    if not expects_logout_control:
        assert 'href="/logout"' not in html
        assert 'action="/logout"' not in html
        assert "Logout" not in html
        return

    if expects_post_form:
        assert (
            '<form class="login-widget__logout-form" method="post" action="/logout">'
            in html
        )
        assert 'type="hidden" name="csrf_token" value="secure-token"' in html
        assert 'href="/logout"' not in html
    else:
        assert (
            '<a class="login-widget__action web-responsive-compact-centre" '
            'href="/logout">'
        ) in html
        assert 'action="/logout"' not in html


def test_dropdown_menu_template_renders_shortcut_metadata() -> None:
    templates = DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.widgets", directory="templates"),
        )
    )
    menu = DropdownPanel(
        label="Settings",
        menu=NavigationMenu(
            label="Settings",
            items=(
                NavigationItem(
                    label="Profile",
                    path="/profile",
                    shortcut=KeyboardShortcut(key="p", label="P", modifiers=("Ctrl",)),
                ),
            ),
            shortcut_scope="settings-menu",
        ),
    )

    html = templates.render_template(
        "components/login_control.html",
        {
            "login_widget": SimpleNamespace(
                authenticated=True,
                login_path=None,
                logout_path=None,
                profile_image=None,
                profile_path="/profile",
                settings_menu=menu,
            ),
            "route_name": "home",
        },
    )

    assert 'data-shortcut-scope="settings-menu"' in html
    assert 'data-shortcut-key="p"' in html
    assert '<kbd class="wybra-navigation-menu__shortcut">Ctrl P</kbd>' in html


def test_navigation_item_records_optional_icon_token() -> None:
    item = NavigationItem(
        label="Profile",
        path="/profile",
        icon_token="user",
    )

    assert item.icon_token == "user"


def test_widget_layout_renders_continuous_header_row() -> None:
    templates = DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.widgets", directory="templates"),
        )
    )

    html = templates.render_template(
        "layouts/page.html",
        {
            "asset_url": lambda path: f"/static/{path}",
            "login_widget": SimpleNamespace(
                authenticated=False,
                login_path="/login",
                logout_path=None,
            ),
            "page_title": "Home",
            "route_name": "home",
            "theme_attribute": None,
            "theme_update_path": None,
        },
    )

    assert '<header class="page-header" aria-label="Page header">' in html
    assert '<div class="page-tools" aria-label="Page controls">' in html
    assert "scripts/widgets.js" not in html
    assert 'href="/login"' in html


def test_widget_layout_omits_header_row_without_controls() -> None:
    templates = DefaultTemplateCapability(
        template_sources=(
            PackageResourceSource(package="wybra.widgets", directory="templates"),
        )
    )

    html = templates.render_template(
        "layouts/page.html",
        {
            "asset_url": lambda path: f"/static/{path}",
            "login_widget": None,
            "page_title": "Home",
            "route_name": "home",
            "theme_attribute": None,
            "theme_update_path": None,
        },
    )

    assert "page-header" not in html
    assert "page-tools" not in html


def test_foundation_styles_expose_header_and_control_tokens() -> None:
    css = _resource_text("wybra.template", "static/styles/app.css")

    for token in (
        "--web-core-font-size-base",
        "--web-core-colour-link",
        "--web-core-colour-highlight",
        "--web-core-colour-secondary",
        "--web-core-colour-header-surface",
        "--web-core-colour-header-border",
        "--web-core-colour-header-text",
        "--web-core-radius-panel",
        "--web-core-radius-button",
        "--web-core-radius-icon",
        "--web-core-page-header-padding",
        "--web-core-page-header-z-index",
        "--web-core-control-size",
    ):
        assert token in css

    assert re.search(
        r"\.container\s+a\s*\{[^}]*color\s*:\s*var\(--web-core-colour-link\)\s*;",
        css,
    )
    assert re.search(r"^a\s*\{", css, flags=re.MULTILINE) is None


def test_widget_styles_use_header_and_control_tokens() -> None:
    css = _resource_text("wybra.widgets", "static/styles/widgets.css")

    for property_name, value in (
        ("background", "var(--web-core-colour-header-surface)"),
        ("border-bottom", "1px solid var(--web-core-colour-header-border)"),
        ("border-radius", "var(--web-core-radius-button)"),
        ("border-radius", "var(--web-core-radius-icon)"),
        ("z-index", "var(--web-core-page-header-z-index)"),
        ("min-height", "var(--web-core-control-size)"),
    ):
        assert _css_declaration_exists(css, property_name, value)

    assert "position: fixed" in css
    assert "inset: auto" in css
    assert ".wybra-dropdown-panel__content[popover]:not(:popover-open)" in css
    assert ".wybra-dropdown-panel__content:popover-open" in css
    assert "left: anchor(right)" in css
    assert "transform: translateX(-100%)" in css


def test_form_styles_right_align_phone_contact_status() -> None:
    css = _resource_text("wybra.forms", "static/styles/forms.css")

    assert re.search(
        r"\.wybra-form-actions\s*\{[^}]*display\s*:\s*grid\s*;",
        css,
    )
    assert re.search(
        r"\.wybra-form-actions\s*\{[^}]*"
        r"grid-template-columns\s*:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)\s*;",
        css,
    )
    assert re.search(
        r"\.wybra-form-action\s*\{[^}]*"
        r"background\s*:\s*var\(--web-core-colour-accent\)\s*;",
        css,
    )
    assert re.search(
        r"\.wybra-form-action--cancel,\s*"
        r"\.wybra-form-action--clear\s*\{[^}]*"
        r"background\s*:\s*var\(--web-core-colour-secondary\)\s*;",
        css,
    )
    assert re.search(
        r"\.wybra-form-action\s*\{[^}]*"
        r"border-radius\s*:\s*var\(--web-core-radius-button\)\s*;",
        css,
    )
    assert re.search(
        r"\.wybra-form-action\s*\{[^}]*"
        r"width\s*:\s*auto\s*;",
        css,
    )
    assert ".wybra-form-actions button.wybra-form-action" in css
    assert re.search(
        r"\.wybra-phone-contact-inline-status\s*\{[^}]*"
        r"justify-content\s*:\s*flex-end\s*;",
        css,
    )
    assert re.search(
        r"\.wybra-phone-contact-inline-status\s*\{[^}]*"
        r"text-align\s*:\s*right\s*;",
        css,
    )


@pytest.mark.anyio
async def test_profile_post_setup_requires_auth_capability() -> None:
    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": ("wybra.profile", "wybra.db"),
                        "database_url": "sqlite+aiosqlite:///profile.sqlite3",
                    },
                }
            ),
        )


@pytest.mark.anyio
async def test_profile_post_setup_requires_database_capability() -> None:
    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": ("wybra.profile", "wybra.auth"),
                        "database_url": "sqlite+aiosqlite:///profile.sqlite3",
                    }
                }
            ),
        )


@pytest.mark.anyio
async def test_profile_post_setup_requires_forms_when_editing_enabled() -> None:
    with pytest.raises(SiteCapabilityError, match="Missing capability"):
        await start(
            FastAPI(),
            config_source=MappingConfigSource(
                {
                    "app": {
                        "modules": ("wybra.profile", "wybra.auth", "wybra.db"),
                        "database_url": "sqlite+aiosqlite:///profile.sqlite3",
                    }
                }
            ),
        )


@pytest.mark.anyio
async def test_profile_image_descriptor_uses_email_initial_without_media(
    tmp_path: Path,
) -> None:
    site = _site_with_database(tmp_path)
    capability = SiteProfileCapability(site.capability_proxy(MediaCapability))
    capability.media.finalise_optional()

    image = await capability.profile_image_for_user(
        ProfileUser(id=uuid.uuid4(), email="_david@example.test")
    )

    assert image.src is None
    assert image.alt == "Profile picture"
    assert image.fallback_text == "D"


@pytest.mark.anyio
async def test_profile_capability_saves_phone_contact(
    tmp_path: Path,
) -> None:
    site = _site_with_database(tmp_path)
    capability = SiteProfileCapability(site.capability_proxy(MediaCapability))
    capability.media.finalise_optional()
    await _create_site_schema(site)
    user_id = uuid.uuid4()

    async with site.require_capability(DatabaseCapability).transaction() as session:
        contact = await capability.save_phone_contact(
            session,
            user_id,
            number="0412 345 678",
            country_code="AU",
            subdivision_code="AU-VIC",
        )

    assert contact.user_id == user_id
    assert contact.country_code == "AU"
    assert contact.subdivision_code == "AU-VIC"
    assert contact.normalised_number == "+61412345678"
    assert contact.number_type == "mobile"
    assert contact.sms_capable is True
    assert contact.verified_at is None


@pytest.mark.anyio
async def test_profile_capability_saves_enabled_profile_fields(
    tmp_path: Path,
) -> None:
    site = _site_with_database(tmp_path)
    capability = SiteProfileCapability(site.capability_proxy(MediaCapability))
    capability.media.finalise_optional()
    await _create_site_schema(site)
    user_id = uuid.uuid4()

    async with site.require_capability(DatabaseCapability).transaction() as session:
        profile = await capability.save_profile_fields(
            session,
            user_id,
            {
                "preferred_name": " David ",
                "display_name": "David Nugent",
                "pronouns": {"direct": "he", "possessive": "his"},
                "profile_links": {"website": "https://example.test/profile"},
                "bio": "Hello <script>alert(1)</script>",
            },
            settings=ProfileSettings(),
        )

    assert profile.user_id == user_id
    assert profile.preferred_name == "David"
    assert profile.display_name == "David Nugent"
    assert profile.pronouns == {"direct": "he", "possessive": "his"}
    assert profile.website_links == {"website": "https://example.test/profile"}
    assert profile.bio == "Hello <script>alert(1)</script>"
    assert (
        render_profile_bio(profile.bio) == "Hello &lt;script&gt;alert(1)&lt;/script&gt;"
    )


@pytest.mark.anyio
async def test_profile_capability_rejects_disabled_profile_fields(
    tmp_path: Path,
) -> None:
    site = _site_with_database(tmp_path)
    capability = SiteProfileCapability(site.capability_proxy(MediaCapability))
    capability.media.finalise_optional()
    await _create_site_schema(site)

    async with site.require_capability(DatabaseCapability).transaction() as session:
        with pytest.raises(ProfileInputError, match="not editable"):
            await capability.save_profile_fields(
                session,
                uuid.uuid4(),
                {"bio": "not allowed"},
                settings=ProfileSettings(editable_fields=("preferred_name",)),
            )


@pytest.mark.anyio
async def test_profile_capability_validates_profile_links_and_bio_length(
    tmp_path: Path,
) -> None:
    site = _site_with_database(tmp_path)
    capability = SiteProfileCapability(site.capability_proxy(MediaCapability))
    capability.media.finalise_optional()
    await _create_site_schema(site)

    async with site.require_capability(DatabaseCapability).transaction() as session:
        with pytest.raises(ProfileInputError, match="URL scheme"):
            await capability.save_profile_fields(
                session,
                uuid.uuid4(),
                {"profile_links": {"website": "javascript:alert(1)"}},
                settings=ProfileSettings(),
            )
        with pytest.raises(ProfileInputError, match="Bio"):
            await capability.save_profile_fields(
                session,
                uuid.uuid4(),
                {"bio": "x" * 1025},
                settings=ProfileSettings(),
            )


@pytest.mark.anyio
async def test_profile_capability_resets_phone_verification_on_number_edit(
    tmp_path: Path,
) -> None:
    site = _site_with_database(tmp_path)
    capability = SiteProfileCapability(site.capability_proxy(MediaCapability))
    capability.media.finalise_optional()
    await _create_site_schema(site)
    user_id = uuid.uuid4()

    async with site.require_capability(DatabaseCapability).transaction() as session:
        contact = UserPhoneContact(
            user_id=user_id,
            country_code="AU",
            normalised_number="+61412345678",
            number_type="mobile",
            sms_capable=True,
            verified_at=1234.0,
        )
        session.add(contact)
        await session.flush()
        edited = await capability.save_phone_contact(
            session,
            user_id,
            contact_id=contact.id,
            number="0412 345 679",
            country_code="AU",
        )

    assert edited.id == contact.id
    assert edited.normalised_number == "+61412345679"
    assert edited.verified_at is None


@pytest.mark.anyio
async def test_profile_capability_recovery_eligibility_requires_verified_unique_sms(
    tmp_path: Path,
) -> None:
    site = _site_with_database(tmp_path)
    capability = SiteProfileCapability(site.capability_proxy(MediaCapability))
    capability.media.finalise_optional()
    await _create_site_schema(site)
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()

    async with site.require_capability(DatabaseCapability).transaction() as session:
        eligible = UserPhoneContact(
            user_id=user_id,
            country_code="AU",
            normalised_number="+61412345678",
            number_type="mobile",
            sms_capable=True,
            verified_at=1234.0,
        )
        duplicate = UserPhoneContact(
            user_id=user_id,
            country_code="AU",
            normalised_number="+61412345679",
            number_type="mobile",
            sms_capable=True,
            verified_at=1234.0,
        )
        other_duplicate = UserPhoneContact(
            user_id=other_user_id,
            country_code="AU",
            normalised_number="+61412345679",
            number_type="mobile",
            sms_capable=True,
            verified_at=1234.0,
        )
        fixed_line = UserPhoneContact(
            user_id=user_id,
            country_code="AU",
            normalised_number="+61370101234",
            number_type="fixed_line",
            sms_capable=False,
            verified_at=1234.0,
        )
        unverified = UserPhoneContact(
            user_id=user_id,
            country_code="AU",
            normalised_number="+61412345670",
            number_type="mobile",
            sms_capable=True,
        )
        session.add_all([eligible, duplicate, other_duplicate, fixed_line, unverified])
        await session.flush()

        contacts = await capability.recovery_eligible_phone_contacts(
            session,
            user_id,
        )

    assert contacts == (eligible,)


@pytest.mark.anyio
async def test_profile_image_descriptor_resolves_media_reference(
    tmp_path: Path,
    create_database_schema: Callable[[FilesystemMediaCapability], Awaitable[None]],
) -> None:
    site = _site_with_database(tmp_path)
    capability = SiteProfileCapability(media=site.capability_proxy(MediaCapability))
    media_capability = FilesystemMediaCapability(
        MediaSettings(root=tmp_path),
        database=site.capability_proxy(DatabaseCapability),
    )
    site.provide_capability(
        MediaCapability,
        media_capability,
    )
    await create_database_schema(media_capability)
    item = await media_capability.register(
        category="profile",
        storage_key="profile/ab/cd/david.png",
        size=10,
    )

    image = await capability.profile_image_for_user(
        ProfileUser(id=uuid.uuid4(), email="david@example.test"),
        UserProfile(user_id=uuid.uuid4(), profile_picture_media_id=item.id),
    )

    assert image.src == "/media/profile/ab/cd/david.png"
    assert image.fallback_text is None


@pytest.mark.anyio
async def test_profile_image_descriptor_falls_back_when_media_is_unavailable(
    tmp_path: Path,
) -> None:
    site = _site_with_database(tmp_path)

    class MissingMedia:
        root = tmp_path
        mount_path = "/media"
        serve = False
        url_mode = "storage-key"

        async def register(
            self,
            *,
            category: str,  # pylint: disable=unused-argument
            storage_key: str,  # pylint: disable=unused-argument
            content_type: str | None = None,  # pylint: disable=unused-argument
            size: int = 0,  # pylint: disable=unused-argument
        ) -> object:
            raise MediaCapabilityError("register not supported for fallback test.")

        async def store(
            self,
            *,
            category: str,  # pylint: disable=unused-argument
            storage_key: str,  # pylint: disable=unused-argument
            upload: object,  # pylint: disable=unused-argument
            chunk_size: int = 0,  # pylint: disable=unused-argument
        ) -> object:
            raise MediaCapabilityError("store not supported for fallback test.")

        async def get(self, media_id: uuid.UUID) -> object:  # pylint: disable=unused-argument
            raise MediaCapabilityError(f"Unknown media item: media_id={media_id}.")

        async def path_for(self, media_id: uuid.UUID) -> Path:  # pylint: disable=unused-argument
            raise MediaCapabilityError(f"Unknown media item: media_id={media_id}.")

        async def url_for(self, media_id: uuid.UUID) -> str:  # pylint: disable=unused-argument
            raise MediaCapabilityError(f"Unknown media item: media_id={media_id}.")

        async def get_by_resource_key(self, resource_key: str) -> object:  # pylint: disable=unused-argument
            raise MediaCapabilityError(f"Unknown media resource key: {resource_key}.")

        async def assign_resource_key(  # pylint: disable=unused-argument
            self,
            media_id: uuid.UUID,
            resource_key: str,
        ) -> None:
            raise MediaCapabilityError(
                f"assign_resource_key not supported: {media_id=}, {resource_key=}."
            )

        def path_for_key(self, storage_key: str | Path) -> Path:
            return Path(storage_key)

        def url_for_key(self, storage_key: str | Path) -> str:
            return f"/media/{storage_key}"

        def validate_writable(self) -> None:
            return None

    site.provide_capability(MediaCapability, MissingMedia())
    capability = SiteProfileCapability(media=site.capability_proxy(MediaCapability))

    image = await capability.profile_image_for_user(
        ProfileUser(id=uuid.uuid4(), email="david@example.test"),
        UserProfile(
            user_id=uuid.uuid4(),
            profile_picture_media_id=uuid.uuid4(),
        ),
    )

    assert image.src is None
    assert image.alt == "Profile picture"
    assert image.fallback_text == "D"


def test_profile_picture_storage_key_uses_profile_category_and_buckets() -> None:
    user_id = uuid.UUID("8ef0c57e-0000-4000-8000-000000000001")

    assert (
        profile_picture_storage_key(user_id, "png")
        == "profile/8e/f0/8ef0c57e000040008000000000000001.png"
    )


@pytest.mark.parametrize("extension", (" ", ".png", "avatar.png", "profile/png", None))
def test_profile_picture_storage_key_rejects_invalid_extensions(
    extension: object,
) -> None:
    with pytest.raises(InputValidationError) as excinfo:
        profile_picture_storage_key(uuid.uuid4(), extension)  # type: ignore[arg-type]

    assert "Profile picture extension" in str(excinfo.value)
    assert isinstance(excinfo.value, ProfileInputError)
    assert not isinstance(excinfo.value, ProfileCapabilityError)
