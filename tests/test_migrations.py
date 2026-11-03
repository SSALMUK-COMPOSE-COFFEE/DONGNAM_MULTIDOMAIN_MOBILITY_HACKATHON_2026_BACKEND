from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from freshswitch_backend.persistence import database
from freshswitch_backend.persistence.database import migration_config
from freshswitch_backend.persistence.tables import ALERT_SEQ, Base


def test_database_is_at_head() -> None:
    head = ScriptDirectory.from_config(migration_config()).get_current_head()
    with database.engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == head


def test_models_have_no_pending_migration() -> None:
    with database.engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []


def test_sequences_are_migrated() -> None:
    with database.engine.connect() as conn:
        assert ALERT_SEQ.name in inspect(conn).get_sequence_names()
