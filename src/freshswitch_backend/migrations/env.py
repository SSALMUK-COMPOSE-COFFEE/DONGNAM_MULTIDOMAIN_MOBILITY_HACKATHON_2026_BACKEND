from alembic import context
from sqlalchemy import Connection, create_engine, pool

from freshswitch_backend.config import get_settings
from freshswitch_backend.persistence.database import DatabaseNotConfigured, normalize_url
from freshswitch_backend.persistence.tables import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = config.get_main_option("sqlalchemy.url") or get_settings().resolved_database_url()
    if not url:
        raise DatabaseNotConfigured("DATABASE_URL이 설정되지 않았습니다")
    return normalize_url(url)


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run(connection)
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as conn:
        _run(conn)
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
