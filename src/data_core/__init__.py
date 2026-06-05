"""Reusable SQLAlchemy and Alembic data infrastructure.

`data_core` may depend on SQLAlchemy, Alembic, and shared composition contracts,
but it must not import host application settings, route modules, or startup code.
"""
