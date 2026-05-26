from pathlib import Path

import pandas as pd
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Driver, Vehicle
from app.services.fleet_import import import_fleet_from_excel


def test_import_idempotent(tmp_path: Path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    xlsx = tmp_path / "fleet.xlsx"
    pd.DataFrame(
        [
            {"Base": "SALVADOR", "Categoria": "VAN", "Motorista": "JOAO", "Placa": "ABC-1234"},
            {"Base": "SALVADOR", "Categoria": "VAN", "Motorista": "JOAO", "Placa": "ABC-1234"},
            {"Base": "SALVADOR", "Categoria": "SUV", "Motorista": "MARIA", "Placa": "DEF-5678"},
        ]
    ).to_excel(xlsx, index=False)

    with Session(engine) as session:
        s1 = import_fleet_from_excel(session, xlsx)
        s2 = import_fleet_from_excel(session, xlsx)

        drivers = session.exec(select(Driver)).all()
        vehicles = session.exec(select(Vehicle)).all()

        assert len(drivers) == 2
        assert len(vehicles) == 2
        assert s1.rows_read == 3
        assert s2.rows_read == 3
