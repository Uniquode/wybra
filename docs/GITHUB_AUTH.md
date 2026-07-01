# GitHub Authentication

This guide is for operators and implementers enabling GitHub login in a
Wybra-based web application. It assumes the application already has Wybra auth,
forms, template, database, migrations, secrets, and provider policy support
working.

The GitHub provider is different from the Google provider. GitHub OAuth Apps do
not return a signed OpenID Connect ID token for this web flow. Wybra must treat
the OAuth access token as a temporary way to call GitHub's REST API, revalidate
the authenticated GitHub user, and then map the immutable GitHub user ID to a
local provider identity link.

The route names and configuration below describe the implementation contract
for the `github-authentication` slice.

## Prerequisites

- A GitHub account that can create OAuth Apps under the target personal account
  or organisation.
- A separate GitHub OAuth App for each environment that needs a different
  callback URL. GitHub OAuth Apps have one configured callback URL.
- A deployed application hostname for production, for example
  `https://app.example.com`.
- A local loopback development hostname if required, for example
  `http://127.0.0.1:8000`.
- Access to the runtime secret store selected by the app configuration.
- The Wybra optional keychain dependency if using `secrets = "keychain"`:

```sh
uv add 'wybra[keychain]'
```

On headless Linux, the keychain source requires a D-Bus session and a Secret
Service provider such as GNOME Keyring, KWallet, KeePassXC, or another
compatible provider.

## OAuth App Or GitHub App

Use a GitHub OAuth App for this login/linking slice.

GitHub Apps are usually the better choice when the application needs
fine-grained repository, organisation, or automation permissions. This Wybra
slice only needs browser-based sign-in, verified email discovery, and a stable
GitHub user identifier, so a GitHub OAuth App is the smaller integration.

Do not request repository, package, workflow, or organisation scopes for basic
login. Add broader GitHub API access only under a separate requirement with its
own consent and storage model.

## Required OAuth Behaviour

Wybra's GitHub provider should use the OAuth web application flow:

1. Redirect the user to `https://github.com/login/oauth/authorize`.
2. Include `client_id`, explicit scopes, `redirect_uri`, CSRF `state`, and PKCE
   `code_challenge` with method `S256`.
3. Receive `code` and `state` on the callback route.
4. Validate state, purpose, expiry, redirect URI, same-browser state cookie, and
   PKCE verifier before accepting the callback.
5. Exchange the code at `https://github.com/login/oauth/access_token` with
   `Accept: application/json`.
6. Validate the token response, token type, and granted scopes.
7. Call GitHub's REST API with the access token to revalidate identity.
8. Resolve the provider assertion through Wybra's provider account policy.

GitHub users can grant fewer scopes than requested and can later edit granted
scopes. The implementation must check the token response `scope` value and
reject callbacks that do not grant the configured required scopes.

## Identity And Email Requirements

Use the GitHub REST API, not profile page data, for identity:

- `GET https://api.github.com/user` supplies the GitHub user record.
- The immutable numeric `id`, converted to a string, is the provider subject
  key.
- `login` is display metadata only because GitHub usernames can change.
- `GET https://api.github.com/user/emails` supplies email addresses and their
  `primary`, `verified`, and `visibility` fields.

Use the default scopes:

```text
read:user user:email
```

`read:user` allows profile reads. `user:email` is required for the emails API
and lets Wybra see verified private email addresses. Do not rely on the
`email` field from `/user`; it can be null when the user's public profile email
is hidden.

For account creation or email-match auto-linking, choose a verified email from
the `/user/emails` response. Prefer the primary verified email. If no verified
email is available, the provider assertion must not satisfy Wybra's verified
email policy, and email-match linking must not proceed.

Successful GitHub login is a primary sign-in method. It does not bypass Wybra
email verification or local TOTP. If the local account is unverified, Wybra
shows the email verification page instead of issuing a session. If local TOTP
is active for the account, the user must still complete local TOTP.

## Create The GitHub OAuth App

In GitHub:

1. Open GitHub and sign in with the account that should own the OAuth App.
2. Open Settings > Developer settings > OAuth Apps.
3. Click New OAuth App, or Register a new application if this is the first app.
4. Enter a public application name.
5. Set Homepage URL to the application URL for this environment.
6. Enter the Authorisation callback URL.
7. Leave Device Flow disabled; Wybra's browser login uses the web application
   flow.
8. Register the application.
9. Copy the client ID.
10. Generate a client secret and store it in the runtime secret store.

Only put information in the OAuth App name, homepage, and description that can
be public.

## Callback URI

The OAuth App must contain the callback URL that Wybra will generate. With the
recommended route mount:

```toml
[app.routes.prefixes."wybra.providers"]
github = "/account/providers/github"
```

the production callback is:

```text
https://app.example.com/account/providers/github/callback
```

The local development callback can use a loopback address:

```text
http://127.0.0.1/account/providers/github/callback
```

When the app runs on a local port, Wybra can send a redirect URI such as:

```text
http://127.0.0.1:8000/account/providers/github/callback
```

GitHub allows loopback redirect URIs to use a different port from the configured
callback URL. Prefer `127.0.0.1` or `::1` for local OAuth loopback testing.

For non-loopback URLs, GitHub requires the provided `redirect_uri` host and port
to match the OAuth App callback URL, and the redirect path must be the callback
path or a subdirectory of it. Because an OAuth App has one callback URL, use
separate OAuth Apps for development, staging, and production.

If you mount the GitHub provider elsewhere:

```toml
[app.routes.prefixes."wybra.providers"]
github = "/oauth/github"
```

then configure GitHub with:

```text
https://app.example.com/oauth/github/callback
```

The callback URI is generated from the incoming request. If the app runs behind
a proxy, configure the proxy/server so the application sees the public scheme
and host.

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
github = "/account/providers/github"

[secrets.keychain]
appname = "my-app"

[auth.providers.github]
enabled = true
client_id = "GITHUB_CLIENT_ID_FROM_GITHUB"
secrets = "keychain"
client_secret_key = "auth/providers/github/client-secret"
account_creation_enabled = false
email_match_linking_enabled = false
required_claims = ["id", "email", "email_verified"]
```

Use environment-specific keys if a shared operator keychain may contain both
development and production values:

```toml
# Development
[auth.providers.github]
enabled = true
client_id = "GITHUB_DEV_CLIENT_ID"
secrets = "keychain"
client_secret_key = "auth/providers/github/dev/client-secret"
account_creation_enabled = true
email_match_linking_enabled = true
required_claims = ["id", "email", "email_verified"]

# Production
# [auth.providers.github]
# enabled = true
# client_id = "GITHUB_PRODUCTION_CLIENT_ID"
# secrets = "keychain"
# client_secret_key = "auth/providers/github/client-secret"
# account_creation_enabled = false
# email_match_linking_enabled = true
# required_claims = ["id", "email", "email_verified"]
```

Policy options:

- `enabled = false`: GitHub login and security-page controls are unavailable.
- `account_creation_enabled = false`: only existing linked GitHub accounts, or
  verified email-match accounts when enabled, can log in.
- `account_creation_enabled = true`: a new GitHub login can create a local
  Wybra account. Provider-created accounts have username/password login
  disabled by default.
- `email_match_linking_enabled = true`: a GitHub login with a verified email
  can match an existing local account by email and persist the GitHub link
  automatically.
- `allowed_domains = ["example.com"]`: restrict provider-created accounts to
  verified GitHub emails in those domains.
- `allowed_emails = ["person@example.com"]`: restrict provider-created
  accounts to specific verified GitHub emails.

Implementation-specific defaults should provide the GitHub endpoints and
default scopes. Configuration should not require operators to repeat them
unless a later requirement adds GitHub Enterprise Server support or custom
endpoint support.

## Secret Store Setup

For the keychain source, the keychain item service/name is
`[secrets.keychain].appname`, and the account/username is the secret key.

Store the GitHub client secret:

```sh
printf '%s' "$GITHUB_CLIENT_SECRET" \
  | uv run wybra-secret --config config/app.toml set auth/providers/github/client-secret
```

For the development key example above:

```sh
printf '%s' "$GITHUB_DEV_CLIENT_SECRET" \
  | uv run wybra-secret --config config/app.toml set auth/providers/github/dev/client-secret
```

Verify that Wybra can see the configured key references:

```sh
uv run wybra-secret --config config/app.toml list --json
uv run wybra-secret --config config/app.toml get --json auth/providers/github/client-secret
```

The `list` command reports Wybra-known keys from configuration; it does not
enumerate every item in the platform keychain.

If the app uses keychain-backed crypto material, also store the system crypto
key references configured under `[secrets.crypto]`:

```toml
[secrets.crypto]
source = "keychain"
current_key = "WYBRA_SECRET_KEY_CURRENT"
previous_keys = "WYBRA_SECRET_KEYS_PREVIOUS"
```

Then store the current key value:

```sh
printf '%s' "$WYBRA_SECRET_KEY_CURRENT" \
  | uv run wybra-secret --config config/app.toml set WYBRA_SECRET_KEY_CURRENT
```

`WYBRA_SECRET_KEYS_PREVIOUS` is optional and is used during key rotation.
Provider token material and TOTP secrets should use stable crypto key material
in production.

An environment-backed provider secret is also supported:

```toml
[auth.providers.github]
enabled = true
client_id = "GITHUB_CLIENT_ID_FROM_GITHUB"
secrets = "environment"
client_secret_key = "GITHUB_CLIENT_SECRET"
account_creation_enabled = false
email_match_linking_enabled = false
required_claims = ["id", "email", "email_verified"]
```

With that configuration, set `GITHUB_CLIENT_SECRET` in the process environment
instead of using `wybra-secret`.

## Implementation Requirements

The GitHub provider implementation should:

- keep GitHub-specific code in focused modules such as
  `wybra.providers.github`, not in package `__init__.py` files;
- derive callback URLs from named routes and configured route prefixes;
- use signed, short-lived, HTTP-only state cookies for login and link starts;
- include PKCE `S256` challenge/verifier handling in state;
- validate callback purpose, expiry, state, redirect URI, PKCE verifier, and
  same-browser state before exchanging a code;
- resolve the client secret through `SecretsCapability`;
- exchange tokens through an injectable token-client boundary;
- request JSON token responses from GitHub;
- validate `token_type = "bearer"` and granted scopes;
- fetch `/user` and `/user/emails` through an injectable GitHub API-client
  boundary;
- use the stringified GitHub `id` as the provider subject;
- use only verified emails for local email verification, email-match linking,
  allowed-domain checks, and allowed-email checks;
- persist provider metadata as non-authoritative display data;
- encrypt provider token material if it is retained;
- complete successful login through Wybra's shared login ceremony;
- reject or degrade cleanly when GitHub is disabled, misconfigured, missing its
  client secret, or returns unusable identity/email data.

## Smoke Checks

Inspect the configured route tree:

```sh
uv run wybra-routes --config config/app.toml --check
uv run wybra-routes --config config/app.toml
```

Expected GitHub route names and paths with the recommended mount:

```text
auth:github-login     /account/providers/github/login
auth:github-link      /account/providers/github/link
auth:github-callback  /account/providers/github/callback
```

Start the app and check:

1. The login page shows `Sign in with GitHub` when GitHub is effectively
   enabled.
2. GitHub redirects back to the configured callback without a callback URL
   error.
3. The callback rejects missing or mismatched state.
4. The callback rejects tokens that do not include the required scopes.
5. A GitHub account with private email still works when the emails API returns
   a verified email.
6. A GitHub account without a verified email does not satisfy email-match
   linking or local email verification.
7. A new GitHub login behaves according to `account_creation_enabled`.
8. Login & Security shows Link GitHub or Unlink GitHub for signed-in users.
9. Unlink GitHub is rejected if it would remove the last usable primary sign-in
   method.
10. If username/password login is enabled and GitHub is linked, Login &
    Security offers an action to disable username/password login.

If the GitHub provider is configured but its keychain client secret is missing,
Wybra should log the provider-specific secret availability problem, disable
GitHub for the effective runtime settings, and continue application startup.

## References

- Creating a GitHub OAuth App:
  `https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app`
- Authorising GitHub OAuth Apps:
  `https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps`
- GitHub OAuth App scopes:
  `https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps`
- GitHub REST API authenticated user endpoint:
  `https://docs.github.com/en/rest/users/users`
- GitHub REST API authenticated user emails endpoint:
  `https://docs.github.com/en/rest/users/emails`
