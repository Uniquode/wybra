# Secret Rotation

Wybra supports explicit rotation for two keychain-backed secret families:

- the `[secrets.crypto]` system secret keyring
- the `wybra.forms` CSRF token signing secret

It does not rotate arbitrary keychain values, OAuth provider client secrets, or
other provider secrets.

Environment variable fallbacks such as `WYBRA_SECRET_KEY` and `CSRF_SECRET_KEY`
are documented in [`ENV_VARS.md`](ENV_VARS.md). They are fallback values, not
automatic rotation targets.

## System Secret Keyring

The system secret keyring is configured under `[secrets.crypto]`. Rotation is
available when the source is `keychain` and both the current and previous key
references are configured:

```toml
[secrets.crypto]
source = "keychain"
current_key = "secrets/key/current"
previous_keys = "secrets/key/previous"
```

Run a dry run first:

```sh
uv run wybra-secret --config config/app.toml rotate system --dry-run
```

Rotate the keyring with:

```sh
uv run wybra-secret --config config/app.toml rotate system
```

Rotation creates a new current key entry, prepends the retired current key to
the previous-keys value, writes the previous-keys value first, then writes the
new current key. This order preserves decryptability if the command is
interrupted between writes.

Existing database values are not rewritten by rotation. They continue to
decrypt because Wybra keeps the retired current key in the previous-keys list.
Do not remove previous keys while stored values may still reference them.

## Re-Encrypt Persisted Secrets

After `rotate system`, you may optionally re-encrypt supported persisted
secret envelopes with the new current key:

```sh
uv run wybra-secret --config config/app.toml reencrypt --dry-run
uv run wybra-secret --config config/app.toml reencrypt
```

This is maintenance, not part of rotation, and it is not required for
decryptability. It scans known reversible `WYBRA:SECRET` database fields,
rewrites values encrypted with previous key versions, skips values already on
the current version, and reports one-way recovery-code verifiers as unsupported.

The initial supported reversible fields are:

- provider access tokens
- provider refresh tokens
- TOTP credential secrets

Recovery-code verifiers are one-way values. They cannot be decrypted or
re-encrypted; rotate or regenerate them through the TOTP credential lifecycle
instead.

## CSRF Token Secret

CSRF token secret rotation is documented in
[`SECRET_KEY.md`](SECRET_KEY.md). The supported command is:

```sh
uv run wybra-secret --config config/app.toml rotate csrf
```

CSRF rotation uses the current storage key
`auth/forms/csrf-token-secret/current` and the previous-secret storage key
`auth/forms/csrf-token-secret/previous`.

## Output

Rotation and re-encryption commands report metadata such as key references,
key versions, counts, and dry-run status. They must not print current keys,
previous keys, CSRF signing secrets, plaintext tokens, encrypted envelopes, or
recovery codes.
