"""Alembic migration environment (BS-018).

Reads the database URL from application settings (DATABASE_URL) rather than
alembic.ini, so credentials never enter version control. Migrations use raw DDL
(the baseline mirrors docs/schema.sql), so target_metadata stays None for now;
ORM autogenerate can be wired up in BS-028 once models exist.
"""

from logging.config import fileConfig

from alembic import context
from config import get_database_url
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the URL from settings (env/.env) into the Alembic config. Migrations need only
# the DB URL — not the full app config/secrets — so this uses the minimal resolver.
config.set_main_option("sqlalchemy.url", get_database_url())

target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB connection (`alembic upgrade --sql`)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
