from pathlib import Path

import pandas as pd
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import Base, Driver, Vehicle
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
        assert all(driver.vehicle_id is not None for driver in drivers)
        assert s1.rows_read == 3
        assert s2.rows_read == 3


def test_import_maps_state_labels_to_existing_bases(tmp_path: Path):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)

    with Session(engine) as session:
        base = Base(name="BA", location="Salvador")
        session.add(base)
        session.commit()
        base_id = base.id

    xlsx = tmp_path / "fleet_state_labels.xlsx"
    pd.DataFrame(
        [
            {"Base": "Bahia", "Categoria": "VAN", "Motorista": "JOAO", "Placa": "ABC-1234"},
            {"Base": "Bahia", "Categoria": "VAN", "Motorista": "JOAO", "Placa": "ABC-1234"},
        ]
    ).to_excel(xlsx, index=False)

    with Session(engine) as session:
        summary = import_fleet_from_excel(session, xlsx)
        driver = session.exec(select(Driver)).first()
        vehicle = session.exec(select(Vehicle)).first()

        assert summary.bases_created == 0
        assert summary.drivers_inserted == 1
        assert summary.vehicles_inserted == 1
        assert driver is not None
        assert vehicle is not None
        assert driver.base_id == base_id
        assert vehicle.base_id == base_id
