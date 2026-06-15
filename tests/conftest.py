import sys

from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session, SQLModel

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import engine, init_db
from app.main import app
from app.services.bootstrap import seed_runtime_data


def _reset_test_db() -> None:
    init_db()
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        for table in reversed(SQLModel.metadata.sorted_tables):
            conn.exec_driver_sql(f'DELETE FROM "{table.name}"')
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")


def _seed_test_data() -> None:
    with Session(engine) as session:
        seed_runtime_data(session)


@pytest.fixture()
def client():
    _reset_test_db()
    with TestClient(app) as test_client:
        _seed_test_data()
        yield test_client
