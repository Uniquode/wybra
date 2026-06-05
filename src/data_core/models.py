from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for reusable SQLAlchemy models."""


metadata = Base.metadata
