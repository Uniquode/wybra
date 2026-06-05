# Auth Extension Migrations

Alembic revision files for `auth_ext` SQLAlchemy models live under
`versions/`.

These revisions are bundled with the module that owns the identity and
authorisation tables. The project migration command discovers this version
location only when `auth_ext` is included in the configured module list.
