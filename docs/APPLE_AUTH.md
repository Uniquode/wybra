# Apple Authentication

This guide is for operators enabling Sign in with Apple in a Wybra-based web
application. It assumes the application already has Wybra auth, forms, template,
database, migrations, secrets, and provider policy support working.

The sections below describe the Apple Developer account setup, Wybra
configuration, secret storage, and smoke checks needed to operate Apple
authentication.

## Prerequisites

- An Apple Developer Program account with Account Holder or Admin access.
- A primary App ID with the Sign in with Apple capability enabled. Apple web
  authentication is associated with a primary iOS, macOS, tvOS, or watchOS App
  ID, even when the website is the only sign-in surface.
- A Services ID for each website or environment that needs a different return
  URL.
- A deployed HTTPS hostname for production, for example `https://app.example.com`.
- An Apple private key for Sign in with Apple, plus its Key ID and the Apple
  Developer Team ID.
- Access to the runtime secret store selected by the app configuration.
- The Wybra optional keychain dependency if using `secrets = "keychain"`:

```sh
uv add 'wybra[keychain]'
```

On headless Linux, the keychain source requires a D-Bus session and a Secret
Service provider such as GNOME Keyring, KWallet, KeePassXC, or another
compatible provider.

## Apple Identifiers

Sign in with Apple for the web uses several Apple Developer identifiers:

- `Team ID`: the Apple Developer team identifier shown in membership details.
- `App ID`: the primary Apple app identifier that owns the Sign in with Apple
  capability.
- `Services ID`: the website identifier. This is the OAuth client identifier
  used by Wybra.
- `Key ID`: the identifier for the Sign in with Apple private key.
- `Private key`: the downloaded `.p8` key file used by the application server.

Use separate Services IDs and Wybra secret-store keys for development, staging,
and production when those environments have different hostnames or operators.
Use separate Apple private keys where your Apple Developer account structure and
key limits allow it.

## Scopes And Email Requirements

Use the default scopes:

```text
name email
```

`email` lets Wybra receive the Apple account email or an Apple private relay
address when the user chooses Hide My Email. `name` lets Apple provide the
user's name during the first successful authorisation.

Apple does not resend the user's name on every login. Treat the name as optional
display information, not as an account identifier.

For account creation or email-match auto-linking, Wybra requires an Apple email
claim that Apple reports as verified. If no verified email is available, Wybra
will not treat the Apple account as satisfying local verified-email policy, and
email-match linking will not proceed.

Private relay addresses use Apple's relay service and usually end in
`privaterelay.appleid.com`. If the application sends account email to users who
choose Hide My Email, configure Apple's private email relay service for the
application's outbound email domain.

Email-match auto-linking only works when Apple's verified email matches the
existing local account email. A private relay address usually will not match an
existing account that was registered with the user's direct email address.

Successful Apple login is a primary sign-in method. It does not bypass Wybra
email verification or local TOTP. If the local account is unverified, Wybra
shows the email verification page instead of issuing a session. If local TOTP
is active for the account, the user must still complete local TOTP.

## Create Or Select The Primary App ID

In the Apple Developer account:

1. Open `https://developer.apple.com/account/resources/identifiers/list`.
2. Create or select the primary App ID that represents the app or website group.
3. Enable the Sign in with Apple capability on that App ID.
4. Save the App ID configuration.

If your organisation already has an iOS, macOS, tvOS, or watchOS app using Sign
in with Apple, use its primary App ID when that is the correct product grouping
for the website. Otherwise create a dedicated primary App ID for this service.

## Create The Services ID

In the Apple Developer account:

1. Open Certificates, Identifiers & Profiles.
2. Open Identifiers.
3. Add a new identifier.
4. Select Services IDs.
5. Enter a description for this website or environment.
6. Enter a reverse-domain identifier, for example `com.example.app.web`.
7. Register the Services ID.
8. Select the Services ID from the identifiers list.
9. Enable Sign in with Apple.
10. Configure Sign in with Apple and choose the primary App ID.
11. Add the website domain and return URL.
12. Save the Services ID configuration.

The Services ID identifier is the Apple OAuth client identifier. In Wybra
configuration, put this value in `client_id`.

## Return URL

The Services ID must contain the return URL that Wybra will generate. With the
recommended route mount:

```toml
[app.routes.prefixes."wybra.providers"]
apple = "/account/providers/apple"
```

the production return URL is:

```text
https://app.example.com/account/providers/apple/callback
```

Apple web authentication is intended for configured website domains and return
URLs. For local testing, prefer a separate Apple Services ID with an HTTPS
development hostname or tunnel that Apple accepts, for example:

```text
https://dev.example.com/account/providers/apple/callback
```

If you mount the Apple provider elsewhere:

```toml
[app.routes.prefixes."wybra.providers"]
apple = "/oauth/apple"
```

then configure Apple with:

```text
https://app.example.com/oauth/apple/callback
```

The return URL is generated from the incoming request. If the app runs behind a
proxy, configure the proxy/server so the application sees the public scheme and
host.

## Create The Apple Private Key

In the Apple Developer account:

1. Open Certificates, Identifiers & Profiles.
2. Open Keys.
3. Add a new key.
4. Enter a key name for the environment.
5. Enable Sign in with Apple for the key.
6. Configure the key and associate it with the primary App ID.
7. Register the key.
8. Copy the Key ID.
9. Download the `.p8` private key file immediately.
10. Store the private key contents in the runtime secret store.

Apple only allows the private key file to be downloaded once. Keep a secure
operator copy outside the source tree, and rotate the key if it is lost or
compromised.

## Private Email Relay

If users may choose Hide My Email and the application sends account email, set
up Apple's private email relay service.

In the Apple Developer account:

1. Open Certificates, Identifiers & Profiles.
2. Open Services.
3. Configure Sign in with Apple for Email Communication.
4. Register the outbound email domain or sending email address used by the app.
5. Ensure the sending domain has the required SPF DNS TXT record.
6. Confirm that Apple reports the email source as passing SPF.

Without this setup, users can still sign in with Apple, but messages sent to
Apple private relay addresses may not reach their personal inboxes.

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
apple = "/account/providers/apple"

[secrets.keychain]
appname = "my-app"

[auth.providers.apple]
enabled = true
client_id = "com.example.app.web"
team_id = "APPLE_TEAM_ID"
key_id = "APPLE_KEY_ID"
secrets = "keychain"
account_creation_enabled = false
email_match_linking_enabled = false
required_claims = ["sub", "email", "email_verified"]
```

With `secrets = "keychain"`, Apple uses the default keychain key
`auth/providers/apple/private-key` unless `private_key_secret_key` is
configured.

Use environment-specific keys if a shared operator keychain may contain both
development and production values:

```toml
# Development
[auth.providers.apple]
enabled = true
client_id = "com.example.app.dev.web"
team_id = "APPLE_TEAM_ID"
key_id = "APPLE_DEV_KEY_ID"
secrets = "keychain"
private_key_secret_key = "auth/providers/apple/dev/private-key"
account_creation_enabled = true
email_match_linking_enabled = true
required_claims = ["sub", "email", "email_verified"]

# Production
# [auth.providers.apple]
# enabled = true
# client_id = "com.example.app.web"
# team_id = "APPLE_TEAM_ID"
# key_id = "APPLE_PRODUCTION_KEY_ID"
# secrets = "keychain"
# account_creation_enabled = false
# email_match_linking_enabled = true
# required_claims = ["sub", "email", "email_verified"]
```

Policy options:

- `enabled = false`: Apple login and security-page controls are unavailable.
- `account_creation_enabled = false`: only existing linked Apple accounts, or
  verified email-match accounts when enabled, can log in.
- `account_creation_enabled = true`: a new Apple login can create a local Wybra
  account. Provider-created accounts have username/password login disabled by
  default.
- `email_match_linking_enabled = true`: an Apple login with a verified email can
  match an existing local account by email and persist the Apple link
  automatically.
- `allowed_domains = ["example.com"]`: restrict provider-created accounts to
  verified Apple emails in those domains. This does not match Apple private
  relay addresses unless the relay domain is explicitly allowed.
- `allowed_emails = ["person@example.com"]`: restrict provider-created accounts
  to specific verified Apple emails.

Wybra provides the Apple authorisation endpoints and default scopes. Operators
usually only need to configure the route mount, Services ID, Team ID, Key ID,
private key secret source, and provider policy options.

## Secret Store Setup

For the keychain source, the keychain item service/name is
`[secrets.keychain].appname`, and the account/username is the secret key.

Store the Apple private key at the default keychain key:

```sh
uv run wybra-secret --config config/app.toml set \
  auth/providers/apple/private-key < AuthKey_APPLE_KEY_ID.p8
```

For the development key example above:

```sh
uv run wybra-secret --config config/app.toml set \
  auth/providers/apple/dev/private-key < AuthKey_APPLE_DEV_KEY_ID.p8
```

Verify that Wybra can see the configured key references:

```sh
uv run wybra-secret --config config/app.toml list --json
uv run wybra-secret --config config/app.toml get --json auth/providers/apple/private-key
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
  | uv run wybra-secret --config config/app.toml set secrets/key/current
```

`previous_keys` points at the keychain value used during key rotation.
Provider token material and TOTP secrets should use stable crypto key material
in production.

An environment-backed provider private key is also supported:

```toml
[auth.providers.apple]
enabled = true
client_id = "com.example.app.web"
team_id = "APPLE_TEAM_ID"
key_id = "APPLE_KEY_ID"
secrets = "environment"
private_key_secret_key = "APPLE_PRIVATE_KEY"
account_creation_enabled = false
email_match_linking_enabled = false
required_claims = ["sub", "email", "email_verified"]
```

With that configuration, set `APPLE_PRIVATE_KEY` in the process environment
instead of using `wybra-secret`. The environment value must contain the full
`.p8` private key contents, including the `BEGIN PRIVATE KEY` and
`END PRIVATE KEY` lines.

## Smoke Checks

Inspect the configured route tree:

```sh
uv run wybra-routes --config config/app.toml --check
uv run wybra-routes --config config/app.toml
```

Expected Apple route names and paths with the recommended mount:

```text
auth:apple-login     /account/providers/apple/login
auth:apple-link      /account/providers/apple/link
auth:apple-callback  /account/providers/apple/callback
```

Start the app and check:

1. The login page shows `Sign in with Apple` when Apple is effectively enabled.
2. Apple redirects back to the configured return URL without a return URL error.
3. A login where the user chooses Hide My Email records the private relay
   address as the Apple email.
4. A new Apple login behaves according to `account_creation_enabled`.
5. Login & Security shows Link Apple or Unlink Apple for signed-in users.
6. Unlink Apple is rejected if it would remove the last usable primary sign-in
   method.
7. If username/password login is enabled and Apple is linked, Login & Security
   offers an action to disable username/password login.
8. If the application sends email, messages to an Apple private relay address
   are delivered after private relay setup is complete.

If the Apple provider is configured but its private key is missing, Apple
sign-in should be unavailable and the logs should identify the missing provider
secret reference.

## References

- About Sign in with Apple:
  `https://developer.apple.com/help/account/capabilities/about-sign-in-with-apple/`
- Register a Services ID:
  `https://developer.apple.com/help/account/identifiers/register-a-services-id/`
- Configure Sign in with Apple for the web:
  `https://developer.apple.com/help/account/capabilities/configure-sign-in-with-apple-for-the-web/`
- Create a Sign in with Apple private key:
  `https://developer.apple.com/help/account/capabilities/create-a-sign-in-with-apple-private-key/`
- Configure private email relay service:
  `https://developer.apple.com/help/account/capabilities/configure-private-email-relay-service/`
