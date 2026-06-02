# ruff: noqa: B018
# Vulture whitelist: framework entry points and future integration hooks.


class _Whitelist:
    pass


_ = _Whitelist()

_.health  # FastAPI route handler registered through APIRouter
_.init_database  # FastAPI lifespan integration hook, wired when DB startup is enabled
_.close_database  # FastAPI lifespan integration hook, wired when DB shutdown is enabled
_.main  # Project script entry point wired through pyproject.toml

# Click command callbacks registered through decorators.
_.create_command
_.update_command
_.delete_command
_.deactivate_command
_.list_command
_.password_command
_.upgrade_command
_.downgrade_command
_.current_command
_.history_command
_.scope_create_command
_.scope_update_command
_.scope_delete_command
_.scope_list_command
_.group_command

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
_.validate_password
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
_.hashed_password
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
_.SCOPE_RECORD_FIELDS
_.primary_table_name
_.list_candidate_child_groups_for_management

# auth_provider public contracts intentionally exported before runtime endpoints.
_.active
_.display_name
_.redirect_uris
_.allowed_scopes
_.is_confidential
_.client_id
_.subject_id
_.redirect_uri
_.scopes
_.family_id
_.verifier
_.issued_at
_.consumed_at
_.revoked_at
_.key_id
_.algorithm
_.public_jwk
_.refresh_tokens
_.resolve_subject
_.get_client
_.save_grant
_.grant
_.consume_grant
_.grant_id
_.has_consent
_.subject
_.client
_.record_consent
_.requested_scopes
_.store_refresh_token
_.get_refresh_token_by_verifier
_.mark_refresh_token_consumed
_.token_id
_.successor
_.revoke_token_family
_.revoke_subject_client_token_families
_.active_signing_key
_.public_signing_keys

# Alembic revision module entry points and metadata.
_.revision
_.down_revision
_.branch_labels
_.depends_on
_.upgrade
_.downgrade

# HTMLParser callback invoked by the standard library.
_.handle_starttag
