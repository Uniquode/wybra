# CSRF Token Secret Key

Wybra forms use a CSRF token secret key to sign CSRF tokens. This secret must
be stable in staging and production so tokens continue to validate across app
reloads and multiple workers.

This key is separate from `[secrets.crypto]` and must not reuse
`WYBRA_SECRET_KEY`.

## Storage Key

When `[wybra.forms].csrf_token_secret_source = "keychain"` is configured, Wybra
uses this canonical current storage key:

```text
auth/forms/csrf-token-secret/current
```

Rolling rotation also uses this canonical previous-secret storage key:

```text
auth/forms/csrf-token-secret/previous
```

The code constants for these storage keys are:

```python
CSRF_TOKEN_SECRET_KEY_CURRENT
CSRF_TOKEN_SECRET_KEY_PREVIOUS
```

Set `[wybra.forms].csrf_token_secret_key` only when intentionally overriding or
migrating the current storage key. If you override the current key, also set
`csrf_token_secret_previous_key` so rotation has a matching previous-secret
location.

## Configuration

Add `wybra.secrets` before `wybra.forms` in the app modules, then enable
keychain-backed CSRF lookup:

```toml
[app]
modules = [
  "wybra.secrets",
  "wybra.forms",
]

[wybra.forms]
csrf_token_secret_source = "keychain"
```

`CSRF_SECRET_KEY` or inline `csrf_token_secret` may be used as a fallback, but the
keychain value wins when it exists.

See [`ENV_VARS.md`](ENV_VARS.md) for the full environment variable reference,
including `CSRF_SECRET_KEY`, `CSRF_SECURE`, and `WYBRA_SECRET_KEY`.

The default CSRF token age is 3600 seconds. Override it only when your
deployment needs a different rotation overlap window:

```toml
[wybra.forms]
csrf_token_max_age_seconds = 3600
```

## Generate A New Secret

Generate and store a new CSRF token secret with:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(32))' \
  | uv run wybra-secret --config config/app.toml set csrf
```

Verify that Wybra can see the key reference:

```sh
uv run wybra-secret --config config/app.toml list
```

For JSON output, `list --json` returns a `keys` object keyed by logical name,
such as `csrf`, `csrf-prev`, `secret`, and `secret-prev`.
Use `list --json --dev` to inspect the built-in development key references.

## Rotation

Run a dry run first:

```sh
uv run wybra-secret --config config/app.toml rotate csrf --dry-run
```

Rotate the CSRF token secret with:

```sh
uv run wybra-secret --config config/app.toml rotate csrf
```

Rotation writes the retired current secret to
`auth/forms/csrf-token-secret/previous` before writing the new current secret.
If previous secrets already exist, the retired current secret becomes the first
entry in that previous-secret list.

Tokens rendered before rotation remain valid only until
`csrf_token_max_age_seconds` expires. Expired tokens are rejected even if they
were signed by a retained previous secret.
