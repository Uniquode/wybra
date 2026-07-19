# Built-in browser forms

Wybra's built-in browser routes use declarative forms where they accept stable
command input. A command form parses and validates input only: the owning auth
or profile service remains responsible for authentication, authorisation,
rate-limiting, token consumption, and persistence.

Ordinary text controls trim outer whitespace. Passwords retain it exactly;
codes and tokens intentionally trim pasted outer whitespace before their
owning security service validates them.

## Native method overrides

The forms module supports native browser `POST` forms that carry one `_method`
value of `PATCH`, `PUT`, or `DELETE` in JSON, URL-encoded, or multipart input.
For safe body replay, override inspection only occurs when the request declares
a valid `Content-Length` no greater than the configured form-body limit.
Lengthless or oversized requests are passed through unchanged, including
chunked uploads; applications that need an override must ensure the client or
proxy supplies a bounded `Content-Length`.

| Surface | Classification | Notes |
| --- | --- | --- |
| Sign-up | Command | Email and password are parsed before the existing account-creation service is called. |
| Password login and TOTP login challenge | Command | Credentials, return destination, challenge identifiers, TOTP/recovery inputs, and the setup-bypass flag are parsed without changing ceremony handling. |
| Password-reset and verification requests/confirmations | Command | Email and opaque token inputs are parsed; anti-enumeration and token outcomes remain service-owned. |
| TOTP setup and security confirmations | Command | Setup identifiers/codes and fresh security assertions are parsed; credential lifecycle and confirmation policy remain service-owned. |
| Provider unlink and passkey revoke | Command | Opaque provider/credential identifiers are parsed before the existing ownership and usable-sign-in checks. |
| Profile details | Model-backed | `UserProfile` fields use a writer-bound `ModelForm`, including its optimistic-lock version token. |
| Profile phone contact | Intentionally bespoke | Country, subdivision, and phone number are a compound control with normalisation, verification state, and a separate persistence path. It is not a fixed one-to-one profile member. |
| Passkey registration and login ceremony payloads | Intentionally bespoke | WebAuthn payloads are browser-ceremony JSON rather than ordinary browser controls. |
| Logout, password-disable, and CSRF-only confirmation posts | Intentionally bespoke | They carry no mutable command data beyond CSRF protection. |
| Theme switcher | Intentionally bespoke | It accepts a small widget-specific URL-encoded request and has a fallback parser for partial rendering environments. |

## Version-field decisions

`VersionField` is intentionally selective. It protects a user-facing editable
record only when the corresponding write path uses a writer-bound `ModelForm`;
ordinary direct Tortoise saves retain their existing last-write-wins behaviour.

| Model group | Decision | Reason |
| --- | --- | --- |
| `User` | Add `VersionField` | Establishes stable account-record schema for future model-backed account editing. Existing auth and admin direct mutations intentionally retain their behaviour. |
| `UserProfile` | Add `VersionField` | Its browser details form is a concurrent user-facing update surface. |
| `UserPhoneContact` | No version | The current compound phone-contact workflow owns normalisation and verification state separately; collection/formset editing is deferred. |
| Auth providers, identity links/emails, TOTP, WebAuthn, recovery codes, authentication challenges, access tokens | No version | These are credential, token, or lifecycle records mutated by controlled services, not general model-form edits. |
| Groups, scopes, and membership join records | No version | They are admin/authorisation records with service-managed transactional updates; no browser model form exists. |
| Initial admin bootstrap | No version | A one-off bootstrap serialisation record. |
| Media items and resource keys | No version | Storage/catalogue records are service-managed; no editable browser model form exists. |
| Message alerts | No version | Queue/alert delivery state, not a user editable record. |
| Session records | No version | High-frequency session infrastructure state. |
