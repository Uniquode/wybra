"""Declarative browser command forms for authentication routes.

These forms parse request input only. Authentication, token verification, rate
limiting, and ceremony handling remain owned by the established auth services.
"""

from __future__ import annotations

from wybra.forms import CheckboxField, Form, HiddenField, TextField


def command_text(form: Form, field_name: str) -> str:
    """Return a validated textual command value, or an empty string."""
    value = form.values.get(field_name)
    return value if isinstance(value, str) else ""


def command_flag(form: Form, field_name: str) -> bool:
    """Return a validated command checkbox value."""
    return form.values.get(field_name) is True


class LoginCommandForm(Form):
    email = TextField(
        label="Email",
        required=False,
        widget="email",
        attr={"autocomplete": "email", "required": True},
    )
    password = TextField(
        label="Password",
        required=False,
        widget="password",
        strip=False,
        allow_html=True,
        attr={"autocomplete": "current-password", "required": True},
    )
    return_to = HiddenField(required=False)
    challenge_id = HiddenField(required=False)
    setup_challenge_id = HiddenField(required=False)
    totp_code = TextField(
        label="Authenticator code",
        required=False,
        attr={"autocomplete": "one-time-code", "required": True},
    )
    recovery_code = TextField(
        label="Recovery code",
        required=False,
        attr={"autocomplete": "one-time-code"},
    )
    bypass_totp_setup = CheckboxField(required=False)


class SignupCommandForm(Form):
    email = TextField(
        label="Email",
        required=False,
        widget="email",
        attr={"autocomplete": "email", "required": True},
    )
    password = TextField(
        label="Password",
        required=False,
        widget="password",
        strip=False,
        allow_html=True,
        attr={"autocomplete": "new-password", "required": True},
    )


class PasswordResetRequestCommandForm(Form):
    email = TextField(
        label="Email",
        required=False,
        widget="email",
        attr={"autocomplete": "email", "required": True},
    )


class PasswordResetConfirmCommandForm(Form):
    token = TextField(
        label="Reset token",
        required=False,
        attr={"autocomplete": "one-time-code", "required": True},
    )
    password = TextField(
        label="New password",
        required=False,
        widget="password",
        strip=False,
        allow_html=True,
        attr={"autocomplete": "new-password", "required": True},
    )


class VerificationRequestCommandForm(Form):
    email = TextField(
        label="Email",
        required=False,
        widget="email",
        attr={"autocomplete": "email", "required": True},
    )


class VerificationConfirmCommandForm(Form):
    token = TextField(
        label="Verification token",
        required=False,
        attr={"autocomplete": "one-time-code", "required": True},
    )


class ProviderUnlinkCommandForm(Form):
    provider_id = HiddenField()


class PasskeyRevokeCommandForm(Form):
    credential_id = HiddenField()


class TotpSetupCommandForm(Form):
    return_to = HiddenField(required=False)
    setup_challenge_id = HiddenField(required=False)
    setup_totp_code = TextField(
        label="Authenticator code",
        required=False,
        attr={
            "autocomplete": "one-time-code",
            "inputmode": "numeric",
            "required": True,
        },
    )


class SecurityAssertionCommandForm(Form):
    password = TextField(
        label="Password",
        required=False,
        widget="password",
        strip=False,
        allow_html=True,
        attr={"autocomplete": "current-password"},
    )
    totp_code = TextField(
        label="Authenticator code",
        required=False,
        attr={"autocomplete": "one-time-code", "inputmode": "numeric"},
    )
    recovery_code = TextField(
        label="Recovery code",
        required=False,
        attr={"autocomplete": "one-time-code"},
    )
    confirmation = TextField(
        label="Password, authenticator code, or recovery code",
        required=False,
        attr={"autocomplete": "one-time-code", "required": True},
    )


__all__ = (
    "LoginCommandForm",
    "PasskeyRevokeCommandForm",
    "PasswordResetConfirmCommandForm",
    "PasswordResetRequestCommandForm",
    "ProviderUnlinkCommandForm",
    "SecurityAssertionCommandForm",
    "SignupCommandForm",
    "TotpSetupCommandForm",
    "VerificationConfirmCommandForm",
    "VerificationRequestCommandForm",
    "command_flag",
    "command_text",
)
