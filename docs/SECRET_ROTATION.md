# Secret Rotation

Wybra supports explicit rotation for two keychain-backed secret families:

- the `[secrets.crypto]` system secret keyring
- the `wybra.forms` CSRF token signing secret

It does not rotate arbitrary keychain values, OAuth provider client secrets, or
other provider secrets.

## System Secret Keyring

The system secret keyring is configured under `[secrets.crypto]`. Rotation is
available when the source is `keychain` and both the current and previous key
references are configured:

```toml
[secrets.crypto]
source = "keychain"
current_key = "WYBRA_SECRET_KEY_CURRENT"
previous_keys = "WYBRA_SECRET_KEYS_PREVIOUS"
```

Run a dry run first:

```sh
uv run wybra-secret --config config/app.toml rotate secret-key --dry-run
```

Rotate the keyring with:

```sh
uv run wybra-secret --config config/app.toml rotate secret-key
```

Rotation creates a new current key entry, prepends the retired current key to
the previous-keys value, writes the previous-keys value first, then writes the
new current key. This order preserves decryptability if the command is
interrupted between writes.

Existing database values are not rewritten by rotation. They continue to
decrypt because Wybra keeps the retired current key in the previous-keys list.
Do not remove previous keys while stored values may still reference them.

## Re-Encrypt Persisted Secrets

After `rotate secret-key`, you may optionally re-encrypt supported persisted
secret envelopes with the new current key:

```sh
uv run wybra-secret --config config/app.toml reencrypt-secrets --dry-run
uv run wybra-secret --config config/app.toml reencrypt-secrets
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
uv run wybra-secret --config config/app.toml rotate csrf-token-secret
```

CSRF rotation uses the current storage key
`auth/forms/csrf-token-secret/current` and the previous-secret storage key
`auth/forms/csrf-token-secret/previous`.

## Output

Rotation and re-encryption commands report metadata such as key references,
key versions, counts, and dry-run status. They must not print current keys,
previous keys, CSRF signing secrets, plaintext tokens, encrypted envelopes, or
recovery codes.
