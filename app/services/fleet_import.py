from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from pathlib import Path

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


STATE_ALIASES = {
    "ACRE": "AC",
    "ALAGOIASAS": "AL",
    "AMAPA": "AP",
    "BAHIA": "BA",
    "CEARA": "CE",
    "DISTRITOFEDERAL": "DF",
    "ESAOPAULOIRITOSANTO": "ES",
    "GOIAS": "GO",
    "PERIOGRANDEDONORTEAMBUCO": "PE",
    "RIOGRANDEDONORTE": "RN",
    "SAOPAULO": "SP",
}

PREFERRED_BASE_LOCATIONS = {
    "AL": "Maceió",
    "BA": "Salvador",
    "MA": "São Luis",
    "PA": "Belém",
    "PB": "João Pessoa",
    "RN": "Natal",
    "RS": "Porto Alegre",
}


def _norm_text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return " ".join(text.split()).upper()


def _norm_key(value: object) -> str:
    text = _norm_text(value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^A-Z0-9]+", "", text)


def _norm_plate(value: object) -> str:
    plate = _norm_text(value).replace(" ", "").replace(".", "")
    return plate.replace("-", "")


def _resolve_base(session: Session, raw_base: object) -> Base:
    base_label = _norm_text(raw_base)
    if not base_label:
        return Base(name="", location="")

    base_key = _norm_key(base_label)
    state_code = STATE_ALIASES.get(base_key, base_label if len(base_label) == 2 else None)

    if state_code:
        bases = session.exec(select(Base).where(Base.name == state_code)).all()
        if bases:
            preferred_location = PREFERRED_BASE_LOCATIONS.get(state_code)
            if preferred_location:
                for base in bases:
                    if _norm_key(base.location) == _norm_key(preferred_location):
                        return base
            return sorted(bases, key=lambda base: (base.location, base.id or 0))[0]
        return Base(name=state_code, location=base_label)

    existing = session.exec(
        select(Base).where(Base.name == base_label, Base.location == base_label)
    ).first()
    if existing:
        return existing
    return Base(name=base_label, location=base_label)


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

        base = _resolve_base(session, base_name)
        if base.id is None:
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
            vehicle = Vehicle(
                plate=plate,
                base_id=base.id,
                vehicle_type=vehicle_type or "NA",
                active=True,
            )
            session.add(vehicle)
            session.flush()
            summary.vehicles_inserted += 1

        if not driver_name:
            continue
        driver = session.exec(
            select(Driver).where(and_(Driver.base_id == base.id, Driver.name == driver_name))
        ).first()
        if driver:
            driver.vehicle_id = vehicle.id
            driver.active = True
            session.add(driver)
            summary.drivers_updated += 1
        else:
            session.add(
                Driver(
                    name=driver_name,
                    phone="",
                    base_id=base.id,
                    vehicle_id=vehicle.id,
                    active=True,
                )
            )
            summary.drivers_inserted += 1

    session.commit()
    return summary
