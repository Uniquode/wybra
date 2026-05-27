import uuid

from fastapi_users_db_sqlalchemy import SQLAlchemyUserDatabase
from sqlalchemy.ext.asyncio import AsyncSession

from auth_ext.sqlalchemy.models import OAuthAccount, User


def create_user_database(
    session: AsyncSession,
) -> SQLAlchemyUserDatabase[User, uuid.UUID]:
    return SQLAlchemyUserDatabase(session, User, OAuthAccount)
