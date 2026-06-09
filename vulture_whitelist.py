# ruff: noqa: B018
# Vulture whitelist: Wevra framework entry points discovered by integrations.


class _Whitelist:
    pass


_ = _Whitelist()

# Package and public helper entry points.
_.__getattr__
_.bootstrap_initial_admin
_.load_composed_settings
_.env_setting_is_set
_.migration_script_root
_.discover_model_metadata
_.redact_database_url
_.register_error_handlers
_.create_fastapi_users
_.optional_current_user
_.require_current_user
_.require_anonymous_user

# Click command callbacks registered through decorators.
_.create_command
_.update_command
_.delete_command
_.deactivate_command
_.list_command
_.password_command
_.init_command
_.revision_command
_.upgrade_command
_.downgrade_command
_.current_command
_.history_command
_.scope_create_command
_.scope_update_command
_.scope_delete_command
_.scope_list_command
_.group_command

# FastAPI Users, schemas, and authentication extension hooks.
_.UserRead
_.UserUpdate
_.validate_password
_.on_after_forgot_password
_.on_after_request_verify
_.AdvancedAuthenticationPolicy
_.NoChallengePolicy
_.after_primary_authentication
_.available_methods
_.requires_challenge
_.complete_challenge

# Protocol, dataclass, and ORM fields used by consumers and SQLAlchemy.
_.additive_route_names
_.replaces
_.hashed_password
_.kind
_.create_challenge
_.TOTPCredentialStore
_.create_pending_totp_credential
_.activate_totp_credential
_.disable_totp_credential
_.WebAuthnCredentialStore
_.store_webauthn_credential
_.credential_id
_.public_key
_.sign_count
_.update_webauthn_sign_count
_.RecoveryCodeStore
_.replace_recovery_codes
_.code_hashes
_.consume_recovery_code
_.code
_.oauth_accounts
_.token_secrets_configured
_.integration_enabled
_.provider_subject
_.crypt_access_token
_.crypt_refresh_token
_.account_email
_.provider_enabled
_.provider_metadata
_.links
_.emails
_.provider_id
_.user_id
_.external_identity_links
_.provider_name
_.is_primary
_.encrypt_required
_.decrypt_required
_.refresh_key_ring
_.from_env
_.from_key_bundle
_.SCOPE_RECORD_FIELDS
_.primary_table_name
_.list_candidate_child_groups_for_management
_.RESERVED_TEMPLATE_CONTEXT_KEYS
_.shadowed
_.totp_enabled
_.passkey_enabled
_.include_prefix
_.accepts_body
_.accepts_form
_.path_parameters
_.template

# Alembic revision module metadata.
_.down_revision
_.branch_labels
_.depends_on

# HTMLParser callback invoked by the standard library.
_.handle_starttag
