from pathlib import Path

from sqlmodel import Session, SQLModel, select

from app.db import engine, init_db
from app.models import Company, User
from app.services.bootstrap import is_database_seeded, seed_runtime_data


def _reset_test_db() -> None:
    init_db()
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for table in reversed(SQLModel.metadata.sorted_tables):
            conn.exec_driver_sql(f'DELETE FROM "{table.name}"')
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def test_is_database_seeded_returns_false_when_empty() -> None:
    _reset_test_db()
    with Session(engine) as session:
        assert not is_database_seeded(session)


def test_seed_runtime_data_is_idempotent_for_existing_users() -> None:
    _reset_test_db()
    with Session(engine) as session:
        seed_runtime_data(session)
        initial_manager_count = len(session.exec(select(User).where(User.email == "gerente@logtudo.local")).all())
        initial_company_count = len(session.exec(select(Company)).all())

        seed_runtime_data(session)
        repeated_manager_count = len(session.exec(select(User).where(User.email == "gerente@logtudo.local")).all())
        repeated_company_count = len(session.exec(select(Company)).all())

        assert initial_manager_count == 1
        assert repeated_manager_count == 1
        assert initial_company_count == repeated_company_count
