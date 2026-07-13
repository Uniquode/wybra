# GitHub Authentication

This guide is for operators enabling GitHub login in a Wybra-based web
application. It assumes the application already has Wybra auth, forms, template,
database, migrations, secrets, and provider policy support working.

The sections below describe the GitHub OAuth App settings, Wybra configuration,
secret storage, and smoke checks needed to operate GitHub authentication.

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

## Scopes And Email Requirements

Use the default scopes:

```text
read:user user:email
```

`read:user` allows Wybra to read the signed-in user's basic GitHub profile.
`user:email` lets Wybra see verified private email addresses when the user's
public profile email is hidden.

For account creation or email-match auto-linking, Wybra requires a verified
GitHub email address. If no verified email is available, Wybra will not treat
the GitHub account as satisfying local verified-email policy, and email-match
linking will not proceed.

GitHub users can grant fewer scopes than requested and can later edit granted
scopes. If a user removes one of the required scopes, GitHub login or linking
will fail until they re-authorise the OAuth App with the required scopes.

GitHub usernames can change. Wybra may show the current GitHub username as
display information, but account matching uses GitHub account and verified email
data instead.

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
account_creation_enabled = false
email_match_linking_enabled = false
required_claims = ["id", "email", "email_verified"]
```

With `secrets = "keychain"`, GitHub uses the default keychain key
`auth/providers/github/client-secret` unless `client_secret_key` is configured.

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

Wybra provides the GitHub OAuth endpoints and default scopes. Operators usually
only need to configure the route mount, client ID, client secret source, and
provider policy options.

## Secret Store Setup

For the keychain source, the keychain item service/name is
`[secrets.keychain].appname`, and the account is the secret key.

Store the GitHub client secret at the default keychain key:

```sh
printf '%s' "$GITHUB_CLIENT_SECRET" \
  | uv run wybra-secret --config config/app.toml set github
```

For the development key example above:

```sh
printf '%s' "$GITHUB_DEV_CLIENT_SECRET" \
  | uv run wybra-secret --config config/app.toml set --dev github
```

Verify that Wybra can see the configured key references:

```sh
uv run wybra-secret --config config/app.toml list --json
uv run wybra-secret --config config/app.toml get --json github
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
3. A GitHub account with private email still works when GitHub returns
   a verified email.
4. A GitHub account without a verified email does not satisfy email-match
   linking or local email verification.
5. A new GitHub login behaves according to `account_creation_enabled`.
6. Login & Security shows Link GitHub or Unlink GitHub for signed-in users.
7. Unlink GitHub is rejected if it would remove the last usable primary sign-in
   method.
8. If username/password login is enabled and GitHub is linked, Login & Security
   offers an action to disable username/password login.

If the GitHub provider is configured but its keychain client secret is missing,
GitHub sign-in should be unavailable and the logs should identify the missing
provider secret reference.

## References

- Creating a GitHub OAuth App:
  `https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app`
- Authorising GitHub OAuth Apps:
  `https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps`
- GitHub OAuth App scopes:
  `https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps`
