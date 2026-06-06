import uuid

from fastapi_users.authentication.strategy.db import DatabaseStrategy
from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from fastapi_users_db_sqlalchemy.access_token import SQLAlchemyAccessTokenDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from wevra.auth.models import AccessToken, OAuthAccount, User
from wevra.auth.options import IdentityOptions


def create_access_token_database(
    session: AsyncSession,
) -> SQLAlchemyAccessTokenDatabase[AccessToken]:
    return SQLAlchemyAccessTokenDatabase(session, AccessToken)


def create_database_strategy(
    session: AsyncSession,
    options: IdentityOptions,
) -> DatabaseStrategy[User, uuid.UUID, AccessToken]:
    return DatabaseStrategy(
        create_access_token_database(session),
        lifetime_seconds=options.session_lifetime_seconds,
    )


async def delete_session_token_by_value(session: AsyncSession, token: str) -> None:
    access_token_database = create_access_token_database(session)
    access_token = await access_token_database.get_by_token(token)
    if access_token is not None:
        await access_token_database.delete(access_token)


def create_user_database(
    session: AsyncSession,
) -> SQLAlchemyUserDatabase[User, uuid.UUID]:
    return SQLAlchemyUserDatabase(session, User, OAuthAccount)
