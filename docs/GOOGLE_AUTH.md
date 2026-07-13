# Google Authentication

This guide is for operators enabling Google login in a Wybra-based web
application. It assumes the application already has Wybra auth, forms, template,
database, and migrations working.

## Prerequisites

- A Google account that can create or administer Google Cloud projects.
- A Google Cloud project for the target environment. Use separate projects, or
  at least separate OAuth clients and Wybra secret keys, for development and
  production.
- A deployed application hostname for production, for example
  `https://app.example.com`.
- A local development hostname if required, for example
  `http://localhost:8000`.
- Access to the runtime secret store selected by the app configuration.
- The Wybra optional keychain dependency if using `secrets = "keychain"`:

```sh
uv add 'wybra[keychain]'
```

On headless Linux, the keychain source requires a D-Bus session and a Secret
Service provider such as GNOME Keyring, KWallet, KeePassXC, or another
compatible provider.

## Google Workspace And Audience Requirements

Google Auth Platform requires an OAuth consent screen before OAuth clients can
be used.

Choose the audience deliberately:

- `Internal`: only available for Google Workspace organisations. Use this when
  only users in that Workspace organisation should sign in.
- `External`: use this when users outside the organisation, including consumer
  Google accounts, may sign in. External apps in testing require configured
  test users. Production external apps may require branding, authorised domains,
  privacy policy and terms URLs, and Google verification depending on the app
  and scopes.

Wybra's Google login uses OpenID Connect with the default scopes:

```text
openid email profile
```

Do not add broader Google API scopes unless the application has a separate
requirement for Google data access. Additional sensitive or restricted scopes
can trigger further Google verification.

## Create The Google Cloud Project

In the Google Cloud console:

1. Open `https://console.cloud.google.com/`.
2. Create or select the project for this environment.
3. Open Google Auth Platform.
4. Configure Branding, Audience, and Data Access.

With the Google Cloud CLI, project creation can be automated:

```sh
gcloud projects create my-app-auth-dev --name="My App Auth Dev"
gcloud config set project my-app-auth-dev
```

Billing is not normally required for Wybra's default Google sign-in flow, but
your organisation may require billing or policy attachment for project
management. Enable product APIs separately if the application later requests
non-login Google API scopes.

## Configure Google Auth Platform

In the selected Google Cloud project:

1. Go to Google Auth Platform > Branding.
2. If Google Auth Platform is not configured, click Get Started.
3. Set the app name and user support email.
4. Choose the audience:
   - `External` for public or cross-organisation login.
   - `Internal` for Google Workspace-only login.
5. Set developer contact email addresses.
6. Accept Google's user data policy.
7. For an external testing app, add test users under Audience.
8. Under Data Access, keep the scopes limited to `openid`, `email`, and
   `profile` unless your app has a separate Google API requirement.
9. Under Branding > Authorised domains, add the top private domain used by the
   app, for example `example.com`.

The authorised domain must be registered before adding production redirect URIs
or app-domain links that use that domain.

## Create The OAuth Client

Create a Web application OAuth client:

1. Go to Google Auth Platform > Clients.
2. Click Create client.
3. Select `Web application`.
4. Name the client for the environment, for example `my-app-dev`.
5. Add authorised redirect URIs.
6. Create the client.
7. Copy the client ID.
8. Copy the client secret immediately and store it in the runtime secret store.

Google only shows the full client secret when it is created. If it is lost,
rotate the client secret and update the Wybra secret store.

## Callback URI

The OAuth client must contain the exact callback URI that Wybra will generate.
With the recommended route mount:

```toml
[app.routes.prefixes."wybra.providers"]
google = "/account/providers/google"
```

the production callback is:

```text
https://app.example.com/account/providers/google/callback
```

The local development callback might be:

```text
http://localhost:8000/account/providers/google/callback
```

Google requires HTTPS for non-localhost redirect URIs. Localhost HTTP redirect
URIs are allowed for development. The redirect URI must match exactly: scheme,
host, port, path, and trailing slash behaviour.

If you mount the Google provider elsewhere, the callback changes with that
mount:

```toml
[app.routes.prefixes."wybra.providers"]
google = "/oauth/google"
```

then configure Google with:

```text
https://app.example.com/oauth/google/callback
```

The callback URI is generated from the incoming request. If the app runs behind
a proxy, configure the proxy/server so the application sees the public scheme
and host.

Wybra does not need an authorised JavaScript origin for the server-side Google
login flow. Add one only if your app separately uses browser-side Google API
JavaScript with the same OAuth client.

## Wybra App Configuration

Enable the secrets and providers modules. `wybra.secrets` should appear before
modules that validate or use secret references.

```toml
[app]
modules = [
  "wybra.secrets",
  "wybra.forms",
  "wybra.assets",
  "wybra.template",
  "wybra.db",
  "wybra.auth",
  "wybra.providers",
]

[app.routes.prefixes."wybra.auth"]
account = "/account"
api = ""

[app.routes.prefixes."wybra.providers"]
google = "/account/providers/google"

[secrets.keychain]
appname = "my-app"

[auth.providers.google]
enabled = true
client_id = "GOOGLE_CLIENT_ID_FROM_GOOGLE"
secrets = "keychain"
account_creation_enabled = false
email_match_linking_enabled = false
required_claims = ["sub", "email", "email_verified"]
```

With `secrets = "keychain"`, Google uses the default keychain key
`auth/providers/google/client-secret` unless `client_secret_key` is configured.

Use environment-specific keys if a shared operator keychain may contain both
development and production values:

```toml
# Development
[auth.providers.google]
enabled = true
client_id = "GOOGLE_DEV_CLIENT_ID"
secrets = "keychain"
client_secret_key = "auth/providers/google/dev/client-secret"
account_creation_enabled = true
email_match_linking_enabled = true
required_claims = ["sub", "email", "email_verified"]

# Production
# [auth.providers.google]
# enabled = true
# client_id = "GOOGLE_PRODUCTION_CLIENT_ID"
# secrets = "keychain"
# account_creation_enabled = false
# email_match_linking_enabled = true
# required_claims = ["sub", "email", "email_verified"]
```

Policy options:

- `enabled = false`: Google login and security-page controls are unavailable.
- `account_creation_enabled = false`: only existing linked Google accounts, or
  verified email-match accounts when enabled, can log in.
- `account_creation_enabled = true`: a new Google login can create a local
  Wybra account. Provider-created accounts have username/password login
  disabled by default.
- `email_match_linking_enabled = true`: a Google login with
  `email_verified = true` can match an existing local account by email and
  persist the Google link automatically.
- `allowed_domains = ["example.com"]`: restrict provider-created accounts to
  verified Google emails in those domains.
- `allowed_emails = ["person@example.com"]`: restrict provider-created
  accounts to specific verified Google emails.

Successful Google login is a primary sign-in method. It does not bypass Wybra
email verification or local TOTP. If the local account is unverified, Wybra
shows the email verification page instead of issuing a session. If local TOTP
is active for the account, the user must still complete local TOTP.

## Secret Store Setup

For the keychain source, the keychain item service/name is
`[secrets.keychain].appname`, and the account is the secret key.

Store the Google client secret at the default keychain key:

```sh
printf '%s' "$GOOGLE_CLIENT_SECRET" \
  | uv run wybra-secret --config config/app.toml set google
```

For the development key example above:

```sh
printf '%s' "$GOOGLE_DEV_CLIENT_SECRET" \
  | uv run wybra-secret --config config/app.toml set --dev google
```

Verify that Wybra can see the configured key references:

```sh
uv run wybra-secret --config config/app.toml list --json
uv run wybra-secret --config config/app.toml get --json google
```

The `list` command reports Wybra-known keys from configuration; it does not
enumerate every item in the platform keychain.

If the app uses keychain-backed crypto material, also store the system crypto
key references configured under `[secrets.crypto]`:

```toml
[secrets.crypto]
source = "keychain"
current_key = "secrets/key/current"
previous_keys = "secrets/key/previous"
```

Then store the current key value:

```sh
printf '%s' "$WYBRA_SECRET_KEY" \
  | uv run wybra-secret --config config/app.toml set secret
```

`previous_keys` points at the keychain value used during key rotation.
Provider token material and TOTP secrets should use stable crypto key material
in production.

An environment-backed provider secret is also supported:

```toml
[auth.providers.google]
enabled = true
client_id = "GOOGLE_CLIENT_ID_FROM_GOOGLE"
secrets = "environment"
client_secret_key = "GOOGLE_CLIENT_SECRET"
account_creation_enabled = false
email_match_linking_enabled = false
required_claims = ["sub", "email", "email_verified"]
```

With that configuration, set `GOOGLE_CLIENT_SECRET` in the process environment
instead of using `wybra-secret`.

## Smoke Checks

Inspect the configured route tree:

```sh
uv run wybra-routes --config config/app.toml --check
uv run wybra-routes --config config/app.toml
```

Expected Google route names and paths with the recommended mount:

```text
auth:google-login     /account/providers/google/login
auth:google-link      /account/providers/google/link
auth:google-callback  /account/providers/google/callback
```

Start the app and check:

1. The login page shows `Sign in with Google` when Google is effectively
   enabled.
2. Google redirects back to the configured callback without
   `redirect_uri_mismatch`.
3. A new Google login behaves according to `account_creation_enabled`.
4. Login & Security shows Link Google or Unlink Google for signed-in users.
5. Unlink Google is rejected if it would remove the last usable primary sign-in
   method.
6. If username/password login is enabled and Google is linked, Login & Security
   offers an action to disable username/password login.

If the Google provider is configured but its keychain client secret is missing,
Wybra logs the provider-specific secret availability problem, disables Google
for the effective runtime settings, and continues application startup.

## References

- Google OAuth 2.0 for web server applications:
  `https://developers.google.com/identity/protocols/oauth2/web-server`
- Google Auth Platform consent and scopes:
  `https://developers.google.com/workspace/guides/configure-oauth-consent`
- Google Auth Platform OAuth clients:
  `https://support.google.com/cloud/answer/15549257`
- Google Auth Platform branding and authorised domains:
  `https://support.google.com/cloud/answer/15549049`
