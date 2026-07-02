# Passkey Authentication

This guide is for operators enabling passkey sign-in in a Wybra-based web
application. It assumes the application already has Wybra auth, forms, template,
database, migrations, and the Login & Security page working.

Passkeys use WebAuthn in the user's browser. There is no OAuth provider account,
client secret, private key, or external developer console to configure. The
operator setup is mainly about stable HTTPS origins, relying-party settings,
browser support, and safe rollout.

## Prerequisites

- A deployed HTTPS hostname for production, for example `https://app.example.com`.
- A stable relying-party ID for the application.
- A user-facing relying-party name, usually the application or organisation
  name shown by browsers and password managers.
- Access to the application's Wybra configuration.
- Database migrations enabled for the auth module.
- Existing user accounts with verified local email addresses before users add
  passkeys.
- Users with supported browsers and authenticators, such as platform passkeys,
  synced passkeys, security keys, Windows Hello, Touch ID, Face ID, Android
  screen lock, or compatible password managers.

Passkeys require a secure browser context. Production deployments must use
HTTPS with a certificate trusted by users' browsers. Browser support for
`localhost` is useful for development, but production passkeys should be tested
on the real public hostname before rollout.

## What Operators Provide

To enable passkeys, an operator must provide:

- `passkey_enabled`: whether passkey registration, management, and login are
  available.
- `rp_id`: the WebAuthn relying-party ID.
- `rp_name`: the human-readable relying-party name.
- `allowed_origins`: the exact public browser origins that may complete
  WebAuthn ceremonies.
- `timeout_seconds`: how long browser registration and authentication
  ceremonies may remain active.
- `user_verification`: whether browsers must require local user verification,
  such as biometric unlock, device PIN, or security-key PIN.
- `user_verification_satisfies_totp`: whether a user-verified passkey sign-in
  satisfies Wybra's local TOTP challenge.
- `attestation`: whether the app asks for authenticator attestation.
- `discoverable_credentials`: whether credentials should be discoverable by the
  authenticator for account-picker or username-less sign-in.
- `counter_policy`: how to handle suspicious signature-counter behaviour.

Operators do not provide secrets for passkeys. WebAuthn stores public-key
credential data, non-sensitive metadata, and replay/counter state in the
application database.

## Relying-Party ID

The relying-party ID is the domain that browsers bind passkeys to. It must be
the current host or a registrable parent domain of the allowed origins.

For a single-host application:

```toml
rp_id = "app.example.com"
allowed_origins = ["https://app.example.com"]
```

For passkeys shared across trusted subdomains:

```toml
rp_id = "example.com"
allowed_origins = [
  "https://app.example.com",
  "https://admin.example.com",
]
```

Choose the relying-party ID deliberately:

- Use the exact app host when passkeys should work only on that host.
- Use the parent domain only when all listed subdomains are trusted parts of
  the same authentication surface.
- Do not use a parent domain if unrelated or untrusted subdomains exist under
  that domain.
- Do not include `https://`, a path, or a port in `rp_id`.
- Do not change `rp_id` after users register passkeys unless you are prepared
  for existing passkeys to stop working.

Adding another allowed origin with the same relying-party ID is a normal
environment change. Changing the relying-party ID is a credential migration
event.

## Allowed Origins

Allowed origins are the exact browser origins that Wybra accepts for WebAuthn
registration and authentication responses. An origin includes scheme, hostname,
and port when a non-default port is used.

Production example:

```toml
allowed_origins = ["https://app.example.com"]
```

Development examples:

```toml
allowed_origins = [
  "http://localhost:8000",
  "http://127.0.0.1:8000",
]
```

Staging and production should use separate configuration:

```toml
# Staging
rp_id = "staging.example.com"
allowed_origins = ["https://staging.example.com"]

# Production
# rp_id = "app.example.com"
# allowed_origins = ["https://app.example.com"]
```

Do not mix staging and production origins in one environment unless the same
database, users, and passkeys are intentionally shared.

## Reverse Proxy And HTTPS

The WebAuthn origin is the public browser origin, not the internal container or
upstream address. If the application runs behind a reverse proxy, configure the
proxy and application server so Wybra sees the public scheme and host.

For production:

- Terminate TLS with a certificate trusted by browsers.
- Forward the public host and scheme to the application.
- Keep the public hostname stable.
- Set secure session-cookie behaviour for the deployed app.
- Avoid serving the same app database from both HTTP and HTTPS origins.

If the app generates WebAuthn challenges from an internal origin such as
`http://127.0.0.1:8000` while users browse `https://app.example.com`, browser
verification will fail.

## Wybra App Configuration

Enable Wybra auth and configure passkey support for the environment.

```toml
[app]
modules = [
  "wybra.forms",
  "wybra.assets",
  "wybra.template",
  "wybra.db",
  "wybra.auth",
]

[app.routes.prefixes."wybra.auth"]
account = "/account"
api = ""

[auth]
passkey_enabled = true
session_cookie_force_secure = true

[auth.passkeys]
rp_id = "app.example.com"
rp_name = "Example App"
allowed_origins = ["https://app.example.com"]
timeout_seconds = 300
user_verification = "required"
user_verification_satisfies_totp = true
attestation = "none"
discoverable_credentials = "preferred"
counter_policy = "reject-regression"
```

Use environment-specific values:

```toml
# Development
[auth]
passkey_enabled = true

[auth.passkeys]
rp_id = "localhost"
rp_name = "Example App Dev"
allowed_origins = ["http://localhost:8000"]
timeout_seconds = 300
user_verification = "preferred"
user_verification_satisfies_totp = true
attestation = "none"
discoverable_credentials = "preferred"
counter_policy = "reject-regression"

# Production
# [auth]
# passkey_enabled = true
# session_cookie_force_secure = true
#
# [auth.passkeys]
# rp_id = "app.example.com"
# rp_name = "Example App"
# allowed_origins = ["https://app.example.com"]
# timeout_seconds = 300
# user_verification = "required"
# user_verification_satisfies_totp = true
# attestation = "none"
# discoverable_credentials = "preferred"
# counter_policy = "reject-regression"
```

Policy options:

- `passkey_enabled = false`: passkey login and Login & Security passkey
  controls are unavailable.
- `user_verification = "required"`: browsers must require local user
  verification, such as biometric unlock, device PIN, or security-key PIN.
  This is recommended for production primary sign-in.
- `user_verification = "preferred"`: browsers should use local user
  verification when available, but may allow authenticators without it. This can
  be useful during development or for broader hardware-key compatibility.
- `user_verification = "discouraged"`: do not use this for primary sign-in
  unless the deployment has a separate explicit policy for possession-only
  authenticators.
- `user_verification_satisfies_totp = true`: a passkey sign-in that includes
  local user verification, such as biometric unlock, device PIN, or
  security-key PIN, satisfies Wybra's local TOTP challenge. This is the default.
- `user_verification_satisfies_totp = false`: accounts with active TOTP must
  complete a separate TOTP challenge after passkey sign-in, even when the
  passkey assertion includes local user verification.
- `attestation = "none"`: do not ask browsers for device attestation. This is
  the recommended default because it avoids device/vendor allow-lists and
  reduces user-identifying hardware signals.
- `attestation = "direct"` or `attestation = "enterprise"`: use only when the
  organisation has a separate policy for approved authenticators and the
  operational process to manage attestation trust.
- `discoverable_credentials = "preferred"`: allow passkeys to be discoverable
  where the authenticator supports it while still supporting account-context
  sign-in.
- `discoverable_credentials = "required"`: require discoverable credentials for
  username-less or account-picker sign-in.
- `counter_policy = "reject-regression"`: reject assertions when a credential's
  signature counter moves backwards.

Passkeys are primary sign-in methods for existing accounts. They do not create
accounts and they do not bypass Wybra email verification. A user must first sign
in to an existing account whose local email address is verified, then add a
passkey from Login & Security. If the local account is unverified, Wybra shows
the email verification page instead of issuing a session.

A passkey assertion that reports user verification satisfies Wybra's local MFA
gate when `user_verification_satisfies_totp = true`. In that case, an account
with active TOTP does not need an additional TOTP challenge. Set
`user_verification_satisfies_totp = false` when the deployment requires a
separate TOTP challenge after passkey sign-in. A valid passkey assertion without
user verification proves credential possession only; if the account or
deployment requires local MFA, Wybra should require another accepted factor such
as TOTP, or reject the login when policy requires user-verified passkeys.

## Database And Migrations

Passkeys require persistent auth database tables for credential public keys,
credential metadata, revocation state, signature counters, and transient
challenge records.

Before enabling passkeys in production:

1. Apply Wybra auth migrations for the deployed version.
2. Confirm the application database is shared by all application instances for
   the environment.
3. Confirm the application can write challenge and credential records.
4. Confirm old challenge records can be expired or cleaned up by normal
   application operation.

Do not enable passkeys on one application instance while another instance
serving the same users runs an older Wybra version that does not understand
passkey credentials.

## Secret Store Setup

Passkey support does not require a provider client secret, private key, or
keychain entry.

The application still needs the normal Wybra auth secrets used for sessions,
CSRF, email verification, password reset, and encrypted auth data where those
features are enabled. Configure those existing auth and secrets settings as
usual for the deployment.

## User Rollout

Recommended rollout:

1. Enable passkeys in a development or staging environment using the same public
   origin shape as production.
2. Register and remove passkeys from Login & Security with several browser and
   authenticator combinations.
3. Confirm passkey login works after signing out, after browser restart, and
   from a second application instance if the deployment is multi-instance.
4. Keep username/password or another primary sign-in method enabled until the
   user has confirmed at least one working passkey.
5. Enable production passkeys for a small operator or staff group first if the
   application has a staged-release process.
6. Only then allow users to disable username/password login when another usable
   primary sign-in method remains.

Passkey registration is user-managed. A user signs in by an existing method,
completes email verification if required, opens Login & Security, and adds a
passkey. Operators do not create passkeys for users, and passkeys are not an
account creation path.

## Smoke Checks

Inspect the configured route tree:

```sh
uv run wybra-routes --config config/app.toml --check
uv run wybra-routes --config config/app.toml
```

Start the app and check:

1. Login & Security shows passkey controls only when passkeys are effectively
   enabled.
2. A signed-in user with verified email can start passkey registration from
   Login & Security.
3. The browser shows the expected application or organisation name during
   registration.
4. A completed registration adds a passkey to the user's Login & Security page.
5. A failed or cancelled browser registration does not add a passkey.
6. The user can sign out and sign back in with the registered passkey.
7. Removing a passkey makes it unavailable for future sign-in.
8. Removing a passkey is rejected if it would remove the user's last usable
   primary sign-in method.
9. If username/password login is enabled and a passkey is registered, Login &
   Security offers an action to disable username/password login.
10. Passkey registration and login fail cleanly when the browser origin is not
    in `allowed_origins`.
11. An email-unverified account cannot start or complete passkey registration.

If passkey controls are hidden, check that `passkey_enabled = true`, the
passkey configuration is valid, the auth routes are mounted, and database
migrations have been applied.

If browser registration fails, check the public browser origin, relying-party
ID, HTTPS certificate, reverse-proxy scheme/host forwarding, and browser
authenticator support.

## References

- Passkeys overview:
  `https://passkeys.dev/docs/`
- WebAuthn relying-party ID and origins:
  `https://www.w3.org/TR/webauthn-3/`
- MDN WebAuthn API:
  `https://developer.mozilla.org/en-US/docs/Web/API/Web_Authentication_API`
