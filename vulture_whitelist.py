# ruff: noqa: B018
# Vulture whitelist: framework entry points and future integration hooks.


class _Whitelist:
    pass


_ = _Whitelist()

_.health  # FastAPI route handler registered through APIRouter
_.init_database  # FastAPI lifespan integration hook, wired when DB startup is enabled
_.close_database  # FastAPI lifespan integration hook, wired when DB shutdown is enabled
_.main  # Project script entry point wired through pyproject.toml

# FastAPI Users and application identity extension hooks.
_.AdvancedAuthenticationPolicy
_.after_primary_authentication
_.available_methods
_.requires_challenge
_.fastapi_users
_.bootstrap_initial_admin
_.created
_.account_creation_policy
_.integration_enabled
_.UserRead
_.UserUpdate
_.on_after_forgot_password
_.on_after_request_verify
_.optional_current_user
_.require_current_user
_.require_anonymous_user

# Protocol, dataclass, and ORM fields used by consumers and SQLAlchemy.
_.additive_route_names
_.replaces
_.id
_.user_id
_.kind
_.expires_at
_.create_challenge
_.create_pending_totp_credential
_.activate_totp_credential
_.disable_totp_credential
_.store_webauthn_credential
_.credential_id
_.public_key
_.sign_count
_.update_webauthn_sign_count
_.replace_recovery_codes
_.code_hashes
_.consume_recovery_code
_.code
_.oauth_accounts
_.csrf_token_secret_configured

# Alembic revision module entry points and metadata.
_.revision
_.down_revision
_.branch_labels
_.depends_on
_.upgrade
_.downgrade

# HTMLParser callback invoked by the standard library.
_.handle_starttag
