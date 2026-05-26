from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
from sqlmodel import Session, and_, select

from app.models import Base, Driver, Vehicle


@dataclass
class ImportSummary:
    rows_read: int = 0
    bases_created: int = 0
    drivers_inserted: int = 0
    drivers_updated: int = 0
    vehicles_inserted: int = 0
    vehicles_updated: int = 0
    rows_skipped: int = 0


def _norm_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return " ".join(text.split()).upper()


def _norm_plate(value: object) -> str:
    plate = _norm_text(value).replace(" ", "").replace(".", "")
    return plate.replace("-", "")


def import_fleet_from_excel(session: Session, xlsx_path: str | Path) -> ImportSummary:
    path = Path(xlsx_path)
    df = pd.read_excel(path)
    summary = ImportSummary(rows_read=len(df))

    for _, row in df.iterrows():
        base_name = _norm_text(row.get("Base"))
        vehicle_type = _norm_text(row.get("Categoria"))
        driver_name = _norm_text(row.get("Motorista"))
        plate = _norm_plate(row.get("Placa"))

        if not base_name or not plate:
            summary.rows_skipped += 1
            continue

        base = session.exec(select(Base).where(Base.name == base_name)).first()
        if not base:
            base = Base(name=base_name, location=base_name)
            session.add(base)
            session.flush()
            summary.bases_created += 1

        vehicle = session.exec(select(Vehicle).where(Vehicle.plate == plate)).first()
        if vehicle:
            vehicle.base_id = base.id
            vehicle.vehicle_type = vehicle_type or vehicle.vehicle_type or "NA"
            vehicle.active = True
            session.add(vehicle)
            summary.vehicles_updated += 1
        else:
            session.add(
                Vehicle(
                    plate=plate,
                    base_id=base.id,
                    vehicle_type=vehicle_type or "NA",
                    active=True,
                )
            )
            summary.vehicles_inserted += 1

        if not driver_name:
            continue
        driver = session.exec(
            select(Driver).where(and_(Driver.base_id == base.id, Driver.name == driver_name))
        ).first()
        if driver:
            driver.active = True
            session.add(driver)
            summary.drivers_updated += 1
        else:
            session.add(
                Driver(
                    name=driver_name,
                    phone="",
                    base_id=base.id,
                    active=True,
                )
            )
            summary.drivers_inserted += 1

    session.commit()
    return summary
