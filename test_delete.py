from app.db import engine
from sqlmodel import Session, select
from app.models import Base

def test_delete():
    with Session(engine) as session:
        # Check if we can delete just one base
        base = session.exec(select(Base)).first()
        if base:
            try:
                session.delete(base)
                session.commit()
                print("Deleted one base successfully")
            except Exception as e:
                print("Error deleting base:", e)

if __name__ == "__main__":
    test_delete()
