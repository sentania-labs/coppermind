"""Alembic environment.

Runs with the synchronous psycopg driver: migrations are a one-shot job, and a
sync engine keeps the failure output readable when a migration fails. The URL
comes from the same wiring the services use, so `alembic upgrade head` needs
no arguments and no hand written connection string.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from coppermind.db.models import Base
from coppermind.settings import Wiring

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    return Wiring().database_url_for("psycopg")


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
