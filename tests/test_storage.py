from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy import BigInteger

from posting_bot.storage import Draft, Editor, ProcessedUpdate, Store


def test_telegram_ids_use_bigint_and_store_large_values(tmp_path):
    assert isinstance(Editor.__table__.c.user_id.type, BigInteger)
    assert isinstance(Draft.__table__.c.user_id.type, BigInteger)
    assert isinstance(ProcessedUpdate.__table__.c.update_id.type, BigInteger)

    store = Store(f"sqlite:///{tmp_path / 'bot.db'}")
    large_user_id = 5_000_000_000
    large_update_id = 6_000_000_000
    with store.session() as session:
        with session.begin():
            draft = store.new(session, large_user_id)
            assert store.mark_update(session, large_update_id)
            draft_id = draft.id

    with store.session() as session:
        assert store.active(session, large_user_id).id == draft_id
        assert session.get(ProcessedUpdate, large_update_id) is not None
    assert store.list_drafts(large_user_id)[0].id == draft_id


def test_existing_postgres_integer_columns_are_upgraded_once():
    old_types = {
        ("editors", "user_id"): "integer",
        ("drafts", "user_id"): "integer",
        ("processed_updates", "update_id"): "integer",
    }
    statements = []

    class Connection:
        def execute(self, _statement, params):
            return SimpleNamespace(
                scalar_one_or_none=lambda: old_types[(params["table_name"], params["column_name"])]
            )

        def exec_driver_sql(self, statement):
            statements.append(statement)
            for (table, column), data_type in old_types.items():
                if statement == f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE BIGINT':
                    assert data_type == "integer"
                    old_types[(table, column)] = "bigint"
                    return
            raise AssertionError(f"Unexpected migration statement: {statement}")

    class Engine:
        dialect = SimpleNamespace(name="postgresql")

        @contextmanager
        def begin(self):
            yield Connection()

    store = object.__new__(Store)
    store.engine = Engine()
    store._upgrade_postgres_integer_ids()
    assert len(statements) == 3
    store._upgrade_postgres_integer_ids()
    assert len(statements) == 3
