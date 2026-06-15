from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

import app.models  # noqa: F401 - ensures SQLModel metadata is populated
from app.seeds import apply_all_seeds

import os
import sys

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "app" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Detect if we are in a test environment and use a separate test database file to protect development data.
is_testing = "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ

if is_testing:
    db_filename = "test.db"
    DB_PATH = DATA_DIR / db_filename
    database_url = f"sqlite:///{DB_PATH}"
    connect_args = {"check_same_thread": False}
else:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        db_filename = "mvp.db"
        DB_PATH = DATA_DIR / db_filename
        database_url = f"sqlite:///{DB_PATH}"
        connect_args = {"check_same_thread": False}
    else:
        connect_args = {}

engine = create_engine(database_url, echo=False, connect_args=connect_args)


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    if engine.dialect.name == "sqlite":
        with engine.begin() as conn:
            # Tables and columns check
            cols = conn.execute(text("PRAGMA table_info(driver)")).fetchall()
            col_names = {c[1] for c in cols}
            if "vehicle_id" not in col_names:
                conn.execute(text("ALTER TABLE driver ADD COLUMN vehicle_id INTEGER REFERENCES vehicle(id)"))
            if "activity_status" not in col_names:
                conn.execute(text("ALTER TABLE driver ADD COLUMN activity_status TEXT DEFAULT 'AVAILABLE'"))
            if "status_updated_at" not in col_names:
                conn.execute(text("ALTER TABLE driver ADD COLUMN status_updated_at TEXT"))
            conn.execute(
                text(
                    "UPDATE driver SET activity_status='AVAILABLE' "
                    "WHERE activity_status IS NULL OR activity_status = ''"
                )
            )

            user_cols = conn.execute(text("PRAGMA table_info('user')")).fetchall()
            user_col_names = {c[1] for c in user_cols}
            if "password_hash" not in user_col_names:
                conn.execute(text("ALTER TABLE 'user' ADD COLUMN password_hash TEXT DEFAULT ''"))
            if "company_name" not in user_col_names:
                conn.execute(text("ALTER TABLE 'user' ADD COLUMN company_name TEXT DEFAULT 'Logtudo'"))
            if "phone" not in user_col_names:
                conn.execute(text("ALTER TABLE 'user' ADD COLUMN phone TEXT"))
            if "job_title" not in user_col_names:
                conn.execute(text("ALTER TABLE 'user' ADD COLUMN job_title TEXT"))
            if "address" not in user_col_names:
                conn.execute(text("ALTER TABLE 'user' ADD COLUMN address TEXT"))

            otp_cols = conn.execute(text("PRAGMA table_info(otpchallenge)")).fetchall()
            otp_col_names = {c[1] for c in otp_cols}
            if "resend_count" not in otp_col_names:
                conn.execute(text("ALTER TABLE otpchallenge ADD COLUMN resend_count INTEGER DEFAULT 0"))
            if "last_resend_at" not in otp_col_names:
                conn.execute(text("ALTER TABLE otpchallenge ADD COLUMN last_resend_at TEXT"))

            cb_cols = conn.execute(text("PRAGMA table_info(companybase)")).fetchall()
            cb_col_names = {c[1] for c in cb_cols}
            if "contract_sla_minutes" not in cb_col_names:
                conn.execute(text("ALTER TABLE companybase ADD COLUMN contract_sla_minutes INTEGER"))

            conf_cols = conn.execute(text("PRAGMA table_info(operationalconfirmation)")).fetchall()
            conf_col_names = {c[1] for c in conf_cols}
            if "driver_id" not in conf_col_names:
                conn.execute(text("ALTER TABLE operationalconfirmation ADD COLUMN driver_id INTEGER REFERENCES driver(id)"))
            if "driver_ids" not in conf_col_names:
                conn.execute(text("ALTER TABLE operationalconfirmation ADD COLUMN driver_ids TEXT"))



def init_db_with_seeds() -> None:
    """Inicializa o banco de dados e aplica todos os seeds."""
    init_db()
    with engine.connect() as conn:
        with Session(engine) as session:
            apply_all_seeds(session, conn)


def get_session():
    with Session(engine) as session:
        yield session
