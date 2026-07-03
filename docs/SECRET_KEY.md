# CSRF Token Secret Key

Wybra forms use a CSRF token secret key to sign CSRF tokens. This secret must
be stable in staging and production so tokens continue to validate across app
reloads and multiple workers.

This key is separate from `[secrets.crypto]` and must not reuse
`WYBRA_SECRET_KEY_CURRENT`.

## Storage Key

When `[wybra.forms].csrf_token_secret_source = "keychain"` is configured, Wybra
uses this canonical storage key:

```text
auth/forms/csrf-token-secret/current
```

The code constant for that storage key is:

```python
CSRF_TOKEN_SECRET_KEY_CURRENT
```

Set `[wybra.forms].csrf_token_secret_key` only when intentionally overriding or
migrating that storage key.

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

`CSRF_SECRET` or inline `csrf_token_secret` may be used as a fallback, but the
keychain value wins when it exists.

## Generate A New Secret

Generate and store a new CSRF token secret with:

```sh
python -c 'import secrets; print(secrets.token_urlsafe(32))' \
  | uv run wybra-secret --config config/app.toml set auth/forms/csrf-token-secret/current
```

Verify that Wybra can see the key reference:

```sh
uv run wybra-secret --config config/app.toml list
```

## Rotation

The storage key is named with `/current` so future CSRF secret rotation can add
previous-key verification without renaming the active key. Current Wybra
versions read only `auth/forms/csrf-token-secret/current` for CSRF tokens.

Replacing the current value immediately invalidates CSRF tokens rendered before
the replacement. That is usually acceptable during a controlled deploy, but it
is not yet rolling rotation.

When CSRF secret rotation is implemented, update this document with the
previous-key storage key, token expiry behaviour, and the rotation procedure.
