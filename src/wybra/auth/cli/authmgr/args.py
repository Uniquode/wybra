from __future__ import annotations

from dataclasses import dataclass

from .passwords import PasswordSource

PROGRAM_NAME = "wybra-authmgr"


@dataclass(slots=True)
class AuthmgrArgs:
    command: str
    email: str = ""
    target: str = ""
    group_target: str = ""
    child_group_target: str = ""
    user_target: str = ""
    scope: str = ""
    description: str | None = None
    add_scopes: tuple[str, ...] = ()
    remove_scopes: tuple[str, ...] = ()
    add_groups: tuple[str, ...] = ()
    remove_groups: tuple[str, ...] = ()
    set_groups: tuple[str, ...] = ()
    password: PasswordSource | None = None
    admin: bool = False
    superuser: bool = False
    unverified: bool = False
    is_admin: bool | None = None
    is_superuser: bool | None = None
    is_verified: bool | None = None
    no_revoke: bool = False
    display_name: str | None = None
    clear_display_name: bool = False
    preferred_name: str | None = None
    clear_preferred_name: bool = False
    preferred_timezone: str | None = None
    clear_preferred_timezone: bool = False
    expires_at: float | None = None
    no_expires_at: bool = False
    force: bool = False
    totp: bool = False
    no_totp: bool = False
    rcodes: bool = False
    include_secrets: bool = False
    json_output: bool = False
    csv_output: bool = False
    email_pattern: str | None = None
    domain_pattern: str | None = None
    effective_active: bool | None = None
    since_created_at: float | None = None
    before_created_at: float | None = None
    since_modified_at: float | None = None
    before_modified_at: float | None = None
    since_last_login_at: float | None = None
    before_last_login_at: float | None = None
    never_logged_in: bool | None = None
    order: str = "email"
    direction: str | None = None
