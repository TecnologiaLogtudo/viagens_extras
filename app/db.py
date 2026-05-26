from pathlib import Path

from sqlalchemy import text
from sqlmodel import Session, SQLModel, create_engine

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "app" / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "mvp.db"

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, connect_args={"check_same_thread": False})


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        cols = conn.execute(text("PRAGMA table_info(driver)")).fetchall()
        col_names = {c[1] for c in cols}
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
        conn.execute(
            text(
                "UPDATE driver SET activity_status = CASE UPPER(activity_status) "
                "WHEN 'AVAILABLE' THEN 'AVAILABLE' "
                "WHEN 'IN_ROUTE' THEN 'IN_ROUTE' "
                "WHEN 'ABSENT' THEN 'ABSENT' "
                "ELSE 'AVAILABLE' END"
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
        conn.execute(text("UPDATE 'user' SET company_name='Logtudo' WHERE company_name IS NULL OR company_name=''"))

        otp_cols = conn.execute(text("PRAGMA table_info(otpchallenge)")).fetchall()
        otp_col_names = {c[1] for c in otp_cols}
        if "resend_count" not in otp_col_names:
            conn.execute(text("ALTER TABLE otpchallenge ADD COLUMN resend_count INTEGER DEFAULT 0"))
        if "last_resend_at" not in otp_col_names:
            conn.execute(text("ALTER TABLE otpchallenge ADD COLUMN last_resend_at TEXT"))
        conn.execute(text("UPDATE otpchallenge SET resend_count=0 WHERE resend_count IS NULL"))


def get_session():
    with Session(engine) as session:
        yield session
