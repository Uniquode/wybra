from __future__ import annotations

from wybra.db.provisioning.mysql import (
    GrantScope,
    MySQLProvisioner,
    _grant_scope_from_show_grants,
)


class MariaDBProvisioner(MySQLProvisioner):
    family = "mariadb"
    label = "MariaDB"
    install_extra = "mariadb"

    def _grant_scope(
        self,
        grant: str,
        *,
        target_database: str,
    ) -> GrantScope:
        return _grant_scope_from_show_grants(
            grant,
            target_database=target_database,
            unsupported_without_scope=True,
        )


__all__ = ("MariaDBProvisioner",)
