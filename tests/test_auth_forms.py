from __future__ import annotations

import pytest
from starlette.datastructures import FormData

from wybra.auth.forms import LoginCommandForm, ProviderUnlinkCommandForm
from wybra.auth.models import User
from wybra.db import VersionField
from wybra.profile.models import UserProfile

pytestmark = pytest.mark.anyio


class TestAuthCommandForms:
    def test_user_and_profile_declare_version_fields(self) -> None:
        assert isinstance(User._meta.fields_map["version"], VersionField)
        assert isinstance(UserProfile._meta.fields_map["version"], VersionField)

    async def test_login_command_form_parses_credentials_and_hidden_inputs(
        self,
    ) -> None:
        form = LoginCommandForm()

        result = await form.parse(
            {
                "email": "person@example.test",
                "password": "correct horse battery staple",
                "return_to": "/account",
                "challenge_id": "challenge-1",
                "totp_code": "123456",
            }
        )

        assert result.is_valid
        assert form.bound_values == {
            "email": "person@example.test",
            "password": "correct horse battery staple",
            "return_to": "/account",
            "challenge_id": "challenge-1",
            "setup_challenge_id": None,
            "totp_code": "123456",
            "recovery_code": None,
            "bypass_totp_setup": False,
        }

    async def test_login_command_form_rejects_markup_and_tracks_unknown_inputs(
        self,
    ) -> None:
        form = LoginCommandForm(unknown_fields="error")

        result = await form.parse(
            {
                "email": "<b>person@example.test</b>",
                "password": "password",
                "unexpected": "value",
            }
        )

        assert not result.is_valid
        assert result.errors["email"] == ("Enter plain text without HTML or markup.",)
        assert result.errors[None] == ("Unknown submitted field(s): unexpected",)

    async def test_login_command_form_preserves_opaque_password_markup(self) -> None:
        form = LoginCommandForm()

        result = await form.parse({"password": "<correct-horse>"})

        assert result.is_valid
        assert form.bound_values["password"] == "<correct-horse>"

    async def test_login_command_form_uses_the_last_duplicate_single_value(
        self,
    ) -> None:
        form = LoginCommandForm()

        result = await form.parse(
            FormData(
                [
                    ("email", "first@example.test"),
                    ("email", "last@example.test"),
                    ("password", "first"),
                    ("password", "last"),
                ]
            )
        )

        assert result.is_valid
        assert form.bound_values["email"] == "last@example.test"
        assert form.bound_values["password"] == "last"

    async def test_login_command_form_parses_rendered_setup_bypass_value(self) -> None:
        form = LoginCommandForm()

        result = await form.parse({"bypass_totp_setup": "1"})

        assert result.is_valid
        assert form.bound_values["bypass_totp_setup"] is True

    def test_login_command_form_declares_browser_presentation_metadata(self) -> None:
        form = LoginCommandForm()

        assert form.fields["email"].widget_name == "email"
        assert form.fields["email"].attr == {
            "autocomplete": "email",
            "required": True,
        }
        assert form.fields["password"].widget_name == "password"
        assert form.fields["password"].attr["autocomplete"] == "current-password"

    async def test_provider_unlink_command_form_requires_a_provider_identifier(
        self,
    ) -> None:
        form = ProviderUnlinkCommandForm()

        result = await form.parse({})

        assert not result.is_valid
        assert result.errors["provider_id"] == ("This field is required.",)
