"""
Alembic environment.

The connection string comes from `app.settings` and nowhere else (CLAUDE.md §4). The
**sync** URL is used deliberately: Alembic's migration runner is synchronous, and
driving it through an async engine means wrapping every migration in a greenlet bridge
for no benefit — migrations are a one-at-a-time operation with no concurrency to win.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app.adapters.db.models import Base
from app.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# What autogenerate compares the database against. Every model module must be imported
# by the time this line runs, or its table is silently missing from the diff — and the
# resulting migration will happily drop a table that exists only because nobody imported it.
target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", get_settings().database_url_sync)


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """
    Keep autogenerate to the objects this project actually owns.

    Without this, any table that exists in a developer's database but not in
    `Base.metadata` is read as a table to *remove* — and the generated migration
    cheerfully proposes dropping it. That is how a scratch table from an integration
    test, or another application sharing the database, ends up deleted by a migration
    nobody thought was destructive.

    `test_` is reserved here for the fixtures the kill-9 test creates at runtime.
    """
    if type_ == "table" and name is not None and name.startswith("test_"):
        return False
    return True


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting. Used to review a migration before applying it."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against the configured database."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Without this, a changed column type is invisible to autogenerate — and a
            # silent type mismatch between the model and the database is the kind of
            # thing that only surfaces under a specific value at 2am.
            compare_type=True,
            compare_server_default=True,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
