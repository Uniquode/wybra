import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from wevra.auth import (
    ERROR_ALREADY_EXISTS,
    ERROR_INVALID_PASSWORD,
    ERROR_PASSWORD_TOO_SHORT,
    ERROR_PASSWORD_TOO_WEAK,
    ChallengeDecision,
    ChallengeKind,
    ChallengeRecord,
    ConfigurationError,
    DefaultPasswordPolicy,
    IdentityIntegration,
    IdentityOptions,
    NoChallengePolicy,
    PasswordStrength,
    PrimaryAuthenticationContext,
    Result,
    RouteReplacement,
    RouterExtensionPlan,
    complete_challenge,
)
from wevra.auth.accounts.manager import public_password_failure_message
from wevra.auth.admin.management import (
    ERROR_CYCLIC_GROUP_MEMBERSHIP,
    ERROR_GROUP_HAS_MEMBERSHIPS,
    ERROR_INVALID_GROUP_ID,
    ERROR_INVALID_USER_ID,
    ERROR_NOT_FOUND,
    ERROR_SCOPE_IN_USE,
    add_child_group_to_group_for_management,
    add_scope_to_group_for_management,
    add_user_to_group_for_management,
    create_group_for_management,
    create_local_user_for_management,
    create_scope_for_management,
    delete_group_for_management,
    delete_scope_for_management,
    effective_scopes_for_user_for_management,
    get_group_for_management,
    list_candidate_child_groups_for_management,
    list_groups_for_management,
    list_scopes_for_management,
    remove_child_group_from_group_for_management,
    remove_scope_from_group_for_management,
    remove_user_from_group_for_management,
    resolve_group_target,
    update_group_for_management,
    update_scope_for_management,
)
from wevra.auth.models import (
    AccessToken,
    Base,
    ExternalIdentityLink,
    GroupGroup,
    GroupScope,
    GroupUser,
    IdentityProvider,
    User,
)
from wevra.auth.models import metadata as wevra_auth_metadata
from wevra.auth.options import VALID_IDENTITY_INTEGRATIONS
from wevra.auth.persistence.database import (
    close_database,
    create_database,
    session_scope,
)
from wevra.auth.routes import normalise_return_to


def sqlite_file_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.resolve().as_posix()}"


async def initialise_auth_database(database_url: str):
    database = create_database(database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return database


class MemoryChallengeStore:
    def __init__(self) -> None:
        self.records: dict[str, ChallengeRecord] = {}
        self.consumed: list[str] = []

    async def create_challenge(
        self,
        user_id: str,
        kind: ChallengeKind,
        expires_at: datetime,
        metadata: dict[str, object] | None = None,
    ) -> ChallengeRecord:
        record = ChallengeRecord(
            id=f"challenge-{len(self.records) + 1}",
            user_id=user_id,
            kind=kind,
            expires_at=expires_at,
            metadata=dict(metadata or {}),
        )
        self.records[record.id] = record
        return record

    async def get_challenge(self, challenge_id: str) -> ChallengeRecord | None:
        return self.records.get(challenge_id)

    async def consume_challenge(self, challenge_id: str) -> None:
        self.consumed.append(challenge_id)
        self.records.pop(challenge_id, None)


def test_wevra_auth_package_is_independent_from_application_modules() -> None:
    source_root = Path(__file__).resolve().parents[1] / "src"

    for package_name in ("wevra.auth",):
        package_root = source_root.joinpath(*package_name.split("."))
        for path in package_root.rglob("*.py"):
            tree = ast.parse(path.read_text(), filename=str(path))
            imported_modules = {
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            } | {
                node.module
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module is not None
            }
            assert not any(
                module == "host_app" or module.startswith("host_app.")
                for module in imported_modules
            )


def test_wevra_auth_metadata_exposes_authorisation_group_tables() -> None:
    assert {
        "identity_group",
        "identity_scope",
        "identity_group_scope",
        "identity_group_user",
        "identity_group_group",
    }.issubset(wevra_auth_metadata.tables)
    assert any(
        constraint.name == "ck_identity_group_group_no_self_membership"
        for constraint in GroupGroup.__table__.constraints
    )
    group_scope_foreign_keys = {
        str(foreign_key.column): foreign_key.ondelete
        for column in GroupScope.__table__.columns
        for foreign_key in column.foreign_keys
    }
    group_user_foreign_keys = {
        str(foreign_key.column): foreign_key.ondelete
        for column in GroupUser.__table__.columns
        for foreign_key in column.foreign_keys
    }
    group_group_foreign_keys = {
        str(foreign_key.column): foreign_key.ondelete
        for column in GroupGroup.__table__.columns
        for foreign_key in column.foreign_keys
    }
    assert group_scope_foreign_keys == {
        "identity_group.id": "RESTRICT",
        "identity_scope.scope": "RESTRICT",
    }
    assert group_user_foreign_keys == {
        "identity_group.id": "RESTRICT",
        "identity_user.id": "CASCADE",
    }
    assert group_group_foreign_keys == {"identity_group.id": "RESTRICT"}


def test_authorisation_scope_management_lifecycle(tmp_path: Path) -> None:
    async def assert_scope_lifecycle() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "scope.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                created = await create_scope_for_management(
                    session,
                    scope="document:read",
                    description="Read documents.",
                )
                duplicate = await create_scope_for_management(
                    session,
                    scope="document:read",
                    description="Duplicate.",
                )
                updated = await update_scope_for_management(
                    session,
                    scope="document:read",
                    description="Read published documents.",
                )
                listed = await list_scopes_for_management(session)
                deleted = await delete_scope_for_management(
                    session,
                    scope="document:read",
                )
                missing = await update_scope_for_management(
                    session,
                    scope="document:read",
                    description="Missing.",
                )

            assert created.is_ok() is True
            assert created.value == {
                "scope": "document:read",
                "description": "Read documents.",
            }
            assert duplicate.is_failure() is True
            assert duplicate.error_type == ERROR_ALREADY_EXISTS
            assert updated.value == {
                "scope": "document:read",
                "description": "Read published documents.",
            }
            assert listed.value == {
                "scopes": [
                    {
                        "scope": "document:read",
                        "description": "Read published documents.",
                    }
                ]
            }
            assert deleted.is_ok() is True
            assert missing.is_failure() is True
            assert missing.error_type == ERROR_NOT_FOUND
        finally:
            await close_database(database)

    asyncio.run(assert_scope_lifecycle())


def test_authorisation_scope_delete_rejects_used_scope(tmp_path: Path) -> None:
    async def assert_used_scope_delete() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "used-scope.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                await create_scope_for_management(session, scope="admin:write")
                await create_group_for_management(
                    session,
                    abbrev="admins",
                    description="Administrators",
                )
                await add_scope_to_group_for_management(
                    session,
                    group_target="admins",
                    scope="admin:write",
                )

                deleted = await delete_scope_for_management(
                    session,
                    scope="admin:write",
                )
                listed = await list_scopes_for_management(session)

            assert deleted.is_failure() is True
            assert deleted.error_type == ERROR_SCOPE_IN_USE
            assert listed.value == {
                "scopes": [{"scope": "admin:write", "description": None}]
            }
        finally:
            await close_database(database)

    asyncio.run(assert_used_scope_delete())


def test_authorisation_group_management_lifecycle(tmp_path: Path) -> None:
    async def assert_group_lifecycle() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "group.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                created = await create_group_for_management(
                    session,
                    abbrev="ops",
                    description="Operations team",
                )
                duplicate = await create_group_for_management(
                    session,
                    abbrev="ops",
                    description="Duplicate",
                )
                resolved_by_abbrev, abbrev_error = await resolve_group_target(
                    session,
                    "ops",
                )
                resolved_by_id, id_error = await resolve_group_target(
                    session,
                    str(created.value["id"]),
                )
                updated = await update_group_for_management(
                    session,
                    target="ops",
                    description="Operations and support",
                )
                shown = await get_group_for_management(session, target="ops")
                listed = await list_groups_for_management(session)
                deleted = await delete_group_for_management(session, target="ops")
                missing = await get_group_for_management(session, target="ops")

            assert created.is_ok() is True
            assert created.value["abbrev"] == "ops"
            assert created.value["description"] == "Operations team"
            assert duplicate.is_failure() is True
            assert duplicate.error_type == ERROR_ALREADY_EXISTS
            assert resolved_by_abbrev is not None
            assert abbrev_error is None
            assert resolved_by_id is not None
            assert id_error is None
            assert updated.value["abbrev"] == "ops"
            assert updated.value["description"] == "Operations and support"
            assert shown.value == updated.value
            assert listed.value == {"groups": [updated.value]}
            assert deleted.value == updated.value
            assert missing.is_failure() is True
            assert missing.error_type == ERROR_INVALID_GROUP_ID
        finally:
            await close_database(database)

    asyncio.run(assert_group_lifecycle())


def test_authorisation_group_target_distinguishes_invalid_from_missing_uuid(
    tmp_path: Path,
) -> None:
    async def assert_group_target_errors() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "group-target.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                invalid_group, invalid_error = await resolve_group_target(
                    session,
                    "not a uuid",
                )
                missing_group, missing_error = await resolve_group_target(
                    session,
                    "00000000-0000-0000-0000-000000000001",
                )

            assert invalid_group is None
            assert invalid_error == ERROR_INVALID_GROUP_ID
            assert missing_group is None
            assert missing_error == ERROR_NOT_FOUND
        finally:
            await close_database(database)

    asyncio.run(assert_group_target_errors())


def test_authorisation_group_scope_assignment_rejects_duplicates(
    tmp_path: Path,
) -> None:
    async def assert_group_scope_assignment() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "group-scope.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                await create_scope_for_management(session, scope="admin:read")
                created_group = await create_group_for_management(
                    session,
                    abbrev="admins",
                    description="Administrators",
                )
                assigned = await add_scope_to_group_for_management(
                    session,
                    group_target="admins",
                    scope="admin:read",
                )
                duplicate = await add_scope_to_group_for_management(
                    session,
                    group_target="admins",
                    scope="admin:read",
                )
                shown = await get_group_for_management(session, target="admins")
                deleted = await delete_group_for_management(
                    session,
                    target=str(created_group.value["id"]),
                )

            assert assigned.is_ok() is True
            assert duplicate.is_failure() is True
            assert duplicate.error_type == ERROR_ALREADY_EXISTS
            assert shown.value["scopes"] == ["admin:read"]
            assert deleted.is_ok() is True
        finally:
            await close_database(database)

    asyncio.run(assert_group_scope_assignment())


def test_authorisation_group_user_membership_rejects_duplicates_and_blocks_delete(
    tmp_path: Path,
) -> None:
    async def assert_user_membership() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "group-user.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                await create_group_for_management(
                    session,
                    abbrev="staff",
                    description="Staff",
                )
                await create_local_user_for_management(
                    session,
                    IdentityOptions(),
                    email="staff@example.com",
                    password="Correct horse 42!",
                )
                assigned = await add_user_to_group_for_management(
                    session,
                    group_target="staff",
                    user_target="staff@example.com",
                )
                duplicate = await add_user_to_group_for_management(
                    session,
                    group_target="staff",
                    user_target="staff@example.com",
                )
                delete_result = await delete_group_for_management(
                    session,
                    target="staff",
                )
                shown = await get_group_for_management(session, target="staff")

            assert assigned.is_ok() is True
            assert duplicate.is_failure() is True
            assert duplicate.error_type == ERROR_ALREADY_EXISTS
            assert delete_result.is_failure() is True
            assert delete_result.error_type == ERROR_GROUP_HAS_MEMBERSHIPS
            assert shown.value["users"] == ["staff@example.com"]
        finally:
            await close_database(database)

    asyncio.run(assert_user_membership())


def test_authorisation_nested_group_membership_rejects_duplicates_and_cycles(
    tmp_path: Path,
) -> None:
    async def assert_nested_group_membership() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "group-group.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                for abbrev in ("parent", "child", "grandchild"):
                    await create_group_for_management(
                        session,
                        abbrev=abbrev,
                        description=f"{abbrev} group",
                    )

                assigned = await add_child_group_to_group_for_management(
                    session,
                    parent_target="parent",
                    child_target="child",
                )
                duplicate = await add_child_group_to_group_for_management(
                    session,
                    parent_target="parent",
                    child_target="child",
                )
                self_membership = await add_child_group_to_group_for_management(
                    session,
                    parent_target="parent",
                    child_target="parent",
                )
                await add_child_group_to_group_for_management(
                    session,
                    parent_target="child",
                    child_target="grandchild",
                )
                cycle = await add_child_group_to_group_for_management(
                    session,
                    parent_target="grandchild",
                    child_target="parent",
                )
                delete_result = await delete_group_for_management(
                    session,
                    target="parent",
                )
                shown = await get_group_for_management(session, target="parent")

            assert assigned.is_ok() is True
            assert duplicate.is_failure() is True
            assert duplicate.error_type == ERROR_ALREADY_EXISTS
            assert self_membership.is_failure() is True
            assert self_membership.error_type == ERROR_CYCLIC_GROUP_MEMBERSHIP
            assert cycle.is_failure() is True
            assert cycle.error_type == ERROR_CYCLIC_GROUP_MEMBERSHIP
            assert delete_result.is_failure() is True
            assert delete_result.error_type == ERROR_GROUP_HAS_MEMBERSHIPS
            assert shown.value["child_groups"] == ["child"]
        finally:
            await close_database(database)

    asyncio.run(assert_nested_group_membership())


def test_authorisation_membership_removal_and_candidate_child_groups(
    tmp_path: Path,
) -> None:
    async def assert_removal_and_candidates() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "group-removal.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                await create_scope_for_management(session, scope="staff:read")
                await create_local_user_for_management(
                    session,
                    IdentityOptions(),
                    email="member@example.com",
                    password="Correct horse 42!",
                )
                for abbrev in ("staff", "child", "candidate"):
                    await create_group_for_management(
                        session,
                        abbrev=abbrev,
                        description=f"{abbrev} group",
                    )
                await add_scope_to_group_for_management(
                    session,
                    group_target="staff",
                    scope="staff:read",
                )
                await add_user_to_group_for_management(
                    session,
                    group_target="staff",
                    user_target="member@example.com",
                )
                await add_child_group_to_group_for_management(
                    session,
                    parent_target="staff",
                    child_target="child",
                )

                candidates = await list_candidate_child_groups_for_management(
                    session,
                    parent_target="staff",
                )
                scope_removed = await remove_scope_from_group_for_management(
                    session,
                    group_target="staff",
                    scope="staff:read",
                )
                user_removed = await remove_user_from_group_for_management(
                    session,
                    group_target="staff",
                    user_target="member@example.com",
                )
                child_removed = await remove_child_group_from_group_for_management(
                    session,
                    parent_target="staff",
                    child_target="child",
                )
                deleted = await delete_group_for_management(
                    session,
                    target="staff",
                )

            assert [group["abbrev"] for group in candidates.value["groups"]] == [
                "candidate"
            ]
            assert scope_removed.is_ok() is True
            assert user_removed.is_ok() is True
            assert child_removed.is_ok() is True
            assert deleted.is_ok() is True
        finally:
            await close_database(database)

    asyncio.run(assert_removal_and_candidates())


def test_effective_scopes_invalid_user_target_returns_invalid_user_id(
    tmp_path: Path,
) -> None:
    async def assert_invalid_user_id() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "effective-invalid-user-id.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                result = await effective_scopes_for_user_for_management(
                    session,
                    user_target="not-a-valid-user-id",
                )

            assert result.is_failure() is True
            assert result.error_type == ERROR_INVALID_USER_ID
            assert (
                result.message
                == "User target must be an email address or valid user ID."
            )
        finally:
            await close_database(database)

    asyncio.run(assert_invalid_user_id())


def test_effective_scopes_missing_user_returns_not_found(tmp_path: Path) -> None:
    async def assert_missing_user() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "effective-missing-user.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                result = await effective_scopes_for_user_for_management(
                    session,
                    user_target="missing.user@example.com",
                )

            assert result.is_failure() is True
            assert result.error_type == ERROR_NOT_FOUND
            assert result.message == "No matching user was found."
        finally:
            await close_database(database)

    asyncio.run(assert_missing_user())


def test_effective_scopes_resolve_direct_nested_and_duplicate_group_scopes(
    tmp_path: Path,
) -> None:
    async def assert_effective_scopes() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "effective.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                await create_local_user_for_management(
                    session,
                    IdentityOptions(),
                    email="scope-user@example.com",
                    password="Correct horse 42!",
                )
                no_groups = await effective_scopes_for_user_for_management(
                    session,
                    user_target="scope-user@example.com",
                )

                for scope in ("document:read", "document:write"):
                    await create_scope_for_management(session, scope=scope)
                for abbrev in ("direct", "nested", "duplicate"):
                    await create_group_for_management(
                        session,
                        abbrev=abbrev,
                        description=f"{abbrev} group",
                    )
                await add_scope_to_group_for_management(
                    session,
                    group_target="direct",
                    scope="document:read",
                )
                await add_scope_to_group_for_management(
                    session,
                    group_target="nested",
                    scope="document:write",
                )
                await add_scope_to_group_for_management(
                    session,
                    group_target="duplicate",
                    scope="document:read",
                )
                await add_user_to_group_for_management(
                    session,
                    group_target="direct",
                    user_target="scope-user@example.com",
                )
                await add_child_group_to_group_for_management(
                    session,
                    parent_target="direct",
                    child_target="nested",
                )
                await add_child_group_to_group_for_management(
                    session,
                    parent_target="nested",
                    child_target="duplicate",
                )

                resolved = await effective_scopes_for_user_for_management(
                    session,
                    user_target="scope-user@example.com",
                )

            assert no_groups.value["scopes"] == []
            assert resolved.value["scopes"] == ["document:read", "document:write"]
            assert resolved.value["groups"] == ["direct", "duplicate", "nested"]
        finally:
            await close_database(database)

    asyncio.run(assert_effective_scopes())


def test_effective_scope_resolution_is_cycle_safe_and_reads_current_data(
    tmp_path: Path,
) -> None:
    async def assert_cycle_safety_and_current_data() -> None:
        database = await initialise_auth_database(
            sqlite_file_url(tmp_path / "effective-current.sqlite3")
        )
        try:
            async with session_scope(database.session_factory) as session:
                await create_local_user_for_management(
                    session,
                    IdentityOptions(),
                    email="current@example.com",
                    password="Correct horse 42!",
                )
                for scope in ("first:read", "second:read"):
                    await create_scope_for_management(session, scope=scope)
                for abbrev in ("first", "second"):
                    await create_group_for_management(
                        session,
                        abbrev=abbrev,
                        description=f"{abbrev} group",
                    )
                await add_scope_to_group_for_management(
                    session,
                    group_target="first",
                    scope="first:read",
                )
                await add_user_to_group_for_management(
                    session,
                    group_target="first",
                    user_target="current@example.com",
                )
                first = await effective_scopes_for_user_for_management(
                    session,
                    user_target="current@example.com",
                )
                first_group, _ = await resolve_group_target(session, "first")
                second_group, _ = await resolve_group_target(session, "second")
                session.add(
                    GroupGroup(
                        parent_group_id=first_group.id,
                        child_group_id=second_group.id,
                    )
                )
                session.add(
                    GroupGroup(
                        parent_group_id=second_group.id,
                        child_group_id=first_group.id,
                    )
                )
                await session.commit()
                before_second_scope = await effective_scopes_for_user_for_management(
                    session,
                    user_target="current@example.com",
                )
                await add_scope_to_group_for_management(
                    session,
                    group_target="second",
                    scope="second:read",
                )
                after_second_scope = await effective_scopes_for_user_for_management(
                    session,
                    user_target="current@example.com",
                )

            assert first.value["scopes"] == ["first:read"]
            assert before_second_scope.value["scopes"] == ["first:read"]
            assert after_second_scope.value["scopes"] == ["first:read", "second:read"]
            assert after_second_scope.value["groups"] == ["first", "second"]
        finally:
            await close_database(database)

    asyncio.run(assert_cycle_safety_and_current_data())


def test_wevra_auth_result_carries_success_values_and_failure_reason() -> None:
    result = Result.ok({"id": "user-1"})

    assert result.is_ok() is True
    assert result.is_failure() is False
    assert result.value == {"id": "user-1"}
    assert result.error_type is None

    failure = Result.failure(ERROR_ALREADY_EXISTS, "Already exists.")

    assert failure.is_failure() is True
    assert failure.is_ok() is False
    assert failure.error_type == ERROR_ALREADY_EXISTS
    assert failure.message == "Already exists."
    assert failure.value is None

    empty_success = Result.ok()

    assert empty_success.is_ok() is True
    assert empty_success.is_failure() is False
    assert empty_success.value is None
    assert empty_success.error_type is None


def test_wevra_auth_default_password_policy_scores_and_accepts_passphrases() -> None:
    policy = DefaultPasswordPolicy()

    strength = policy.strength("correct horse")
    validation = policy.validate("correct horse")

    assert strength.score >= policy.minimum_score
    assert strength.label in {"fair", "good", "strong"}
    assert validation.is_ok() is True


@pytest.mark.parametrize(
    ("password", "error_type"),
    [
        ("   ", ERROR_INVALID_PASSWORD),
        ("short 1", ERROR_PASSWORD_TOO_SHORT),
        ("admin password 123!", ERROR_PASSWORD_TOO_WEAK),
        ("changeme 123!", ERROR_PASSWORD_TOO_WEAK),
        ("changeit 123!", ERROR_PASSWORD_TOO_WEAK),
        ("p4ssw0rd 123!", ERROR_PASSWORD_TOO_WEAK),
        ("pass phrase 123!", ERROR_PASSWORD_TOO_WEAK),
        ("tester account 123!", ERROR_PASSWORD_TOO_WEAK),
        ("test account 123!", ERROR_PASSWORD_TOO_WEAK),
        ("abcdefghijkl", ERROR_PASSWORD_TOO_WEAK),
    ],
)
def test_wevra_auth_default_password_policy_rejects_invalid_values(
    password: str,
    error_type: str,
) -> None:
    result = DefaultPasswordPolicy().validate(password)

    assert result.is_failure() is True
    assert result.error_type == error_type
    assert result.message


@pytest.mark.parametrize(
    ("password", "user"),
    [
        (
            "signup 12345!",
            SimpleNamespace(email="signup@example.com"),
        ),
        (
            "operator 123!",
            SimpleNamespace(display_name="Operator Example"),
        ),
        (
            "david 123456!",
            SimpleNamespace(preferred_name="David"),
        ),
        (
            "identity 123!",
            SimpleNamespace(username="identity-admin"),
        ),
    ],
)
def test_wevra_auth_default_password_policy_rejects_account_detail_fragments(
    password: str,
    user: object,
) -> None:
    result = DefaultPasswordPolicy().validate(password, user)

    assert result.is_failure() is True
    assert result.error_type == ERROR_PASSWORD_TOO_WEAK
    assert result.message


def test_wevra_auth_identity_options_accept_custom_password_policy() -> None:
    class RejectingPasswordPolicy:
        def strength(
            self,
            password: str,
            user: object | None = None,
        ) -> PasswordStrength:
            del password, user
            return PasswordStrength(
                score=0.0,
                label="weak",
                feedback=("Rejected by custom policy.",),
            )

        def validate(
            self,
            password: str,
            user: object | None = None,
        ) -> Result[str]:
            del password, user
            return Result.failure(
                ERROR_PASSWORD_TOO_WEAK,
                "Rejected by custom policy.",
            )

    options = IdentityOptions(password_policy=RejectingPasswordPolicy())
    validation = options.resolved_password_policy().validate("correct horse")

    assert validation.is_failure() is True
    assert validation.error_type == ERROR_PASSWORD_TOO_WEAK
    assert validation.message == "Rejected by custom policy."


def test_wevra_auth_integration_options_enabled() -> None:
    options = IdentityOptions(
        **{
            f"{integration}_enabled": True
            for integration in VALID_IDENTITY_INTEGRATIONS
        },
    )

    for integration in VALID_IDENTITY_INTEGRATIONS:
        assert options.integration_enabled(integration) is True


def test_wevra_auth_identity_options_integration_enabled_rejects_unknown() -> None:
    options = IdentityOptions()
    unknown_integration: str = "sso"

    with pytest.raises(ConfigurationError):
        options.integration_enabled(
            cast(IdentityIntegration, unknown_integration),
        )


def test_wevra_auth_password_failure_message_filters_unrecognised_reasons() -> None:
    assert (
        public_password_failure_message(
            "Internal breach provider matched tenant-specific denylist."
        )
        == "Password is invalid."
    )
    assert (
        public_password_failure_message(
            "Password does not meet the strength requirement."
        )
        == "Password does not meet the strength requirement."
    )


def test_wevra_auth_no_challenge_policy_allows_direct_login() -> None:
    async def assert_policy() -> None:
        decision = await NoChallengePolicy().after_primary_authentication(
            PrimaryAuthenticationContext(user_id="user-1"),
            MemoryChallengeStore(),
        )

        assert isinstance(decision, ChallengeDecision)
        assert decision.requires_challenge is False
        assert decision.challenge is None

    asyncio.run(assert_policy())


def test_wevra_auth_identity_provider_and_link_models_are_well_formed() -> None:
    provider_columns = set(IdentityProvider.__table__.columns.keys())
    external_identity_link_columns = set(ExternalIdentityLink.__table__.columns.keys())
    access_token_columns = set(AccessToken.__table__.columns.keys())

    assert {
        "provider_name",
        "provider_subject",
        "crypt_access_token",
        "crypt_refresh_token",
        "account_email",
    }.issubset(provider_columns)
    assert {"user_id", "provider_id"}.issubset(external_identity_link_columns)
    assert {"token", "created_at", "user_id"}.issubset(access_token_columns)

    assert IdentityProvider.__tablename__ == "identity_provider"
    assert AccessToken.__tablename__ == "identity_access_token"
    assert ExternalIdentityLink.__tablename__ == "identity_external_identity_link"

    assert {
        str(foreign_key.column)
        for foreign_key in ExternalIdentityLink.__table__.columns[
            "user_id"
        ].foreign_keys
    } == {"identity_user.id"}
    assert {
        str(foreign_key.column)
        for foreign_key in ExternalIdentityLink.__table__.columns[
            "provider_id"
        ].foreign_keys
    } == {"identity_provider.id"}
    assert {
        str(foreign_key.column)
        for foreign_key in AccessToken.__table__.columns["user_id"].foreign_keys
    } == {"identity_user.id"}
    assert User.external_identity_links.property.mapper.class_ is ExternalIdentityLink


def test_wevra_auth_challenge_completion_consumes_existing_challenge() -> None:
    async def assert_challenge_completion() -> None:
        store = MemoryChallengeStore()
        challenge = await store.create_challenge(
            user_id="user-1",
            kind="totp",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )

        assert await complete_challenge(store, challenge.id) is True
        assert challenge.id in store.consumed
        assert await complete_challenge(store, challenge.id) is False

    asyncio.run(assert_challenge_completion())


def test_wevra_auth_router_extension_plan_tracks_explicit_replacements() -> None:
    plan = RouterExtensionPlan(
        additive_route_names=("wevra.auth:challenge",),
        replacements=(
            RouteReplacement(
                method="POST",
                path="/login",
                reason="Pause primary login for MFA challenge.",
            ),
        ),
    )

    assert plan.replaces("post", "/login") is True
    assert plan.replaces("GET", "/login") is False


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (None, "/account"),
        ("", "/account"),
        ("/dashboard", "/dashboard"),
        ("/dashboard?tab=billing", "/dashboard?tab=billing"),
        ("/dashboard#ignored", "/dashboard"),
        ("https://evil.example/account", "/account"),
        ("//evil.example/account", "/account"),
        ("/%2f%2fevil.example/account", "/account"),
        ("/%5cevil.example/account", "/account"),
        ("/account%0d%0aLocation:%20https://evil.example", "/account"),
    ),
)
def test_normalise_return_to_accepts_only_local_redirect_paths(
    value: str | None,
    expected: str,
) -> None:
    assert normalise_return_to(value) == expected
