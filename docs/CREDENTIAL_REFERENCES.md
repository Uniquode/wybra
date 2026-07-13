# Credential References

Credential references let module settings describe secrets that an operator may
need to create, inspect, rotate, or validate. They expose metadata only. They
must never include resolved credential values.

Every settings class inherits `BaseSettings.credential_references()`. Modules
without credentials use the default empty tuple. Modules that own credentials
override it and return one or more `CredentialReference` values.

Each reference includes:

| Field | Description |
| --- | --- |
| `name` | Stable Wybra-defined logical name used by tools such as `wybra-secret`. |
| `key` | Configured secret-store key or environment variable name. |
| `owner` | Module or subsystem that owns the credential. |
| `description` | Operator-facing description of the credential. |
| `source` | Secret source used to resolve the key. |
| `required` | Whether the credential is required when the owning feature is active. |
| `rotation_role` | Optional `current` or `previous` marker for rotatable credentials. |

Names are a Wybra API. They must be unique across configured references. A
duplicate name is a programming error and should fail fast so `wybra-secret`
cannot target the wrong credential.

References are discovered from loaded module settings. Callers should aggregate
references through the existing settings/configuration path instead of
re-parsing raw TOML or environment data.

Database settings use this contract for runtime and service-account database
credentials. Crypto, forms, and authentication provider settings use the same
shape for keychain-backed secrets.
