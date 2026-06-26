"""
Script to update drivers and vehicles database from COT D2D 24-06.xlsx.

This script maps bases, handles formatting mismatches, matches drivers
using name similarity and plates, creates new drivers/vehicles where needed,
and updates existing records.
"""

import argparse
from datetime import datetime, timezone
import os
import sys
import unicodedata
import pandas as pd
from sqlmodel import Session, select

# Ensure project root is in sys.path
sys.path.insert(0, os.getcwd())

from app.db import engine
from app.models import Base, Driver, Vehicle

# Mapping of Excel base descriptions to DB state and location
BASE_MAPPING = {
    "AL - MACEIO": ("AL", "Rio Largo"),
    "BA - ILHÉUS": ("BA", "Ilhéus"),
    "BA - ILHUS": ("BA", "Ilhéus"),
    "BA - PORTO SEGURO": ("BA", "Porto Seguro"),
    "BA - SALVADOR": ("BA", "Salvador"),
    "BA - VITÓRIA DA CONQUISTA": ("BA", "Vitória da Conquista"),
    "BA - VITRIA DA CONQUISTA": ("BA", "Vitória da Conquista"),
    "ES - VITORIA": ("ES", "Vitória"),
    "MA - IMPERATRIZ": ("MA", "Imperatriz"),
    "PA - BELEM": ("PA", "Belém"),
    "PB - J. PESSOA": ("PB", "Bayeux"),
    "PI - TERESINA": ("PI", "Teresina"),
    "PORTO ALEGRE": ("RS", "Porto Alegre"),
    "SE - ARACAJU": ("SE", "Aracaju"),
    "SE - SE - ARACAJU": ("SE", "Aracaju"),
}


def normalize_str(value: str) -> str:
    """Normalize string: uppercase, remove accents, and strip spaces."""
    if not value or not isinstance(value, str):
        return ""
    value = value.strip().upper()
    normalized = "".join(
        c for c in unicodedata.normalize("NFD", value)
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(normalized.split())


def clean_plate(plate: str) -> str:
    """Remove spaces and hyphens from vehicle plates."""
    if not plate or not isinstance(plate, str):
        return ""
    return plate.replace(" ", "").replace("-", "").strip().upper()


def clean_name_for_match(name: str) -> str:
    """Clean driver name for matching, removing any trailing metadata."""
    normalized = normalize_str(name)
    if " - " in name:
        parts = name.split(" - ")
        normalized = normalize_str(parts[0])
    return normalized


def run_update(excel_path: str, dry_run: bool = False):
    """Process the Excel file and apply updates to the database."""
    print(f"Loading data from: {excel_path}")
    df = pd.read_excel(excel_path, sheet_name="Motoristas")
    print(f"Total rows in spreadsheet: {len(df)}")

    with Session(engine) as session:
        # Load all DB records to build in-memory caches
        db_bases = session.exec(select(Base)).all()
        db_drivers = session.exec(select(Driver)).all()
        db_vehicles = session.exec(select(Vehicle)).all()

        # Cache bases by (normalized_name, normalized_location)
        bases_by_key = {}
        for base in db_bases:
            key = (normalize_str(base.name), normalize_str(base.location))
            bases_by_key[key] = base

        # Cache vehicles by cleaned plate
        vehicles_by_plate = {clean_plate(v.plate): v for v in db_vehicles}

        # Cache drivers by vehicle_id
        drivers_by_vehicle_id = {
            d.vehicle_id: d for d in db_drivers if d.vehicle_id
        }

        # Cache drivers by normalized name
        drivers_by_name = {normalize_str(d.name): d for d in db_drivers}

        counters = {
            "skipped": 0,
            "drivers_updated": 0,
            "drivers_created": 0,
            "vehicles_updated": 0,
            "vehicles_created": 0,
            "no_change": 0,
        }

        print("\nProcessing rows...")

        for idx, row in df.iterrows():
            raw_name = str(row.get("Nome", ""))
            raw_plate = str(row.get("Placa", ""))
            raw_base = str(row.get("Base", ""))
            raw_cat = str(row.get("Categoria", ""))

            # Clean and normalize
            clean_n = clean_name_for_match(raw_name)
            clean_p = clean_plate(raw_plate)
            clean_c = raw_cat.strip()

            if not clean_n or not clean_p or not raw_base:
                print(f"Row {idx}: Missing critical data (Name/Plate/Base). Skipping.")
                counters["skipped"] += 1
                continue

            # 1. Map Base
            mapped_base_key = BASE_MAPPING.get(raw_base.strip())
            if not mapped_base_key:
                # Fallback mapping
                parts = [p.strip() for p in raw_base.split("-")]
                if len(parts) >= 2:
                    mapped_base_key = (normalize_str(parts[0]), normalize_str(parts[-1]))
                else:
                    mapped_base_key = (normalize_str(raw_base), normalize_str(raw_base))

            db_base = bases_by_key.get(
                (normalize_str(mapped_base_key[0]), normalize_str(mapped_base_key[1]))
            )

            if not db_base:
                print(f"Row {idx}: Could not map base '{raw_base}'. Skipping.")
                counters["skipped"] += 1
                continue

            # 2. Driver Matching Logic
            matched_driver = drivers_by_name.get(clean_n)

            # Check if vehicle exists
            db_veh = vehicles_by_plate.get(clean_p)

            # If not matched by name, try plate-based similarity match
            if not matched_driver and db_veh:
                db_drv_by_plate = drivers_by_vehicle_id.get(db_veh.id)
                if db_drv_by_plate:
                    norm_db_name = clean_name_for_match(db_drv_by_plate.name)
                    words_excel = set(clean_n.split())
                    words_db = set(norm_db_name.split())
                    # Check if there is overlapping words (e.g. short name vs full name)
                    if words_excel.intersection(words_db):
                        matched_driver = db_drv_by_plate
                        print(
                            f"Row {idx}: Driver '{raw_name}' matched to DB Driver "
                            f"'{matched_driver.name}' (ID {matched_driver.id}) by plate similarity."
                        )

            # 3. Vehicle Creation/Update
            vehicle_is_new = False
            vehicle_changed = False

            if not db_veh:
                # Create vehicle if not exists
                db_veh = Vehicle(
                    plate=clean_p,
                    vehicle_type=clean_c,
                    base_id=db_base.id,
                    active=True,
                )
                session.add(db_veh)
                session.flush()  # populate ID
                vehicles_by_plate[clean_p] = db_veh
                vehicle_is_new = True
                counters["vehicles_created"] += 1
                print(f"Row {idx}: Created new Vehicle '{clean_p}' ({clean_c}) for Base ID {db_base.id}.")
            else:
                # Update existing vehicle base or type if they changed
                veh_updates = []
                if db_veh.base_id != db_base.id:
                    db_veh.base_id = db_base.id
                    veh_updates.append(f"base_id -> {db_base.id}")
                if db_veh.vehicle_type != clean_c:
                    db_veh.vehicle_type = clean_c
                    veh_updates.append(f"vehicle_type -> '{clean_c}'")
                if not db_veh.active:
                    db_veh.active = True
                    veh_updates.append("active -> True")

                if veh_updates:
                    session.add(db_veh)
                    vehicle_changed = True
                    counters["vehicles_updated"] += 1
                    print(f"Row {idx}: Updated Vehicle '{clean_p}': {', '.join(veh_updates)}")

            # 4. Handle Driver Record
            if matched_driver:
                driver_changed = False
                driver_updates = []

                # Update driver's name to the more complete Excel name if it has more words
                # and doesn't lose accents unnecessarily.
                # Only update name if it matches similar name logic and Excel has full name.
                excel_words_count = len(raw_name.strip().split())
                db_words_count = len(matched_driver.name.split())
                # If Excel name is longer and does not contain special characters/abbreviations, update it
                if (
                    excel_words_count > db_words_count
                    and " - " not in raw_name
                    and normalize_str(matched_driver.name) in normalize_str(raw_name)
                ):
                    old_name = matched_driver.name
                    matched_driver.name = raw_name.strip()
                    driver_updates.append(f"name: '{old_name}' -> '{matched_driver.name}'")

                # Update base
                if matched_driver.base_id != db_base.id:
                    matched_driver.base_id = db_base.id
                    driver_updates.append(f"base_id: -> {db_base.id}")

                # Update vehicle link
                if matched_driver.vehicle_id != db_veh.id:
                    # If this vehicle was linked to another driver, unlink them
                    old_drv_of_veh = drivers_by_vehicle_id.get(db_veh.id)
                    if old_drv_of_veh and old_drv_of_veh.id != matched_driver.id:
                        old_drv_of_veh.vehicle_id = None
                        session.add(old_drv_of_veh)
                        print(
                            f"Row {idx}: Unlinked Vehicle '{clean_p}' from DB Driver "
                            f"'{old_drv_of_veh.name}' (ID {old_drv_of_veh.id})."
                        )
                        # update cache
                        drivers_by_vehicle_id[db_veh.id] = None

                    matched_driver.vehicle_id = db_veh.id
                    driver_updates.append(f"vehicle_id -> {db_veh.id} (Plate: {db_veh.plate})")

                # Ensure active
                if not matched_driver.active:
                    matched_driver.active = True
                    driver_updates.append("active -> True")

                if driver_updates:
                    session.add(matched_driver)
                    session.flush()
                    # update caches
                    drivers_by_vehicle_id[db_veh.id] = matched_driver
                    drivers_by_name[normalize_str(matched_driver.name)] = matched_driver
                    counters["drivers_updated"] += 1
                    print(
                        f"Row {idx}: Updated Driver '{matched_driver.name}' "
                        f"(ID {matched_driver.id}): {', '.join(driver_updates)}"
                    )
                else:
                    if not vehicle_is_new and not vehicle_changed:
                        counters["no_change"] += 1
            else:
                # Create new driver
                # Unlink vehicle if it was linked to someone else
                old_drv_of_veh = drivers_by_vehicle_id.get(db_veh.id)
                if old_drv_of_veh:
                    old_drv_of_veh.vehicle_id = None
                    session.add(old_drv_of_veh)
                    print(
                        f"Row {idx}: Unlinked Vehicle '{clean_p}' from DB Driver "
                        f"'{old_drv_of_veh.name}' (ID {old_drv_of_veh.id}) to link to new driver."
                    )
                    # update cache
                    drivers_by_vehicle_id[db_veh.id] = None

                new_driver = Driver(
                    name=raw_name.strip(),
                    phone="",
                    base_id=db_base.id,
                    vehicle_id=db_veh.id,
                    active=True,
                    status_updated_at=datetime.now(timezone.utc),
                )
                session.add(new_driver)
                session.flush()

                # update caches
                drivers_by_vehicle_id[db_veh.id] = new_driver
                drivers_by_name[normalize_str(new_driver.name)] = new_driver
                counters["drivers_created"] += 1
                print(f"Row {idx}: Created new Driver '{new_driver.name}' (ID {new_driver.id}) linked to Vehicle ID {db_veh.id}.")

        if dry_run:
            print("\n[DRY RUN] Rolling back transactions.")
            session.rollback()
        else:
            print("\nCommitting changes to database...")
            session.commit()
            print("Successfully updated database!")

        print("\n--- RESULTS SUMMARY ---")
        print(f"Drivers Updated:   {counters['drivers_updated']}")
        print(f"Drivers Created:   {counters['drivers_created']}")
        print(f"Vehicles Updated:  {counters['vehicles_updated']}")
        print(f"Vehicles Created:  {counters['vehicles_created']}")
        print(f"Rows Skipped:      {counters['skipped']}")
        print(f"No Change Needed:  {counters['no_change']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import/Update drivers and vehicles from Excel.")
    parser.add_argument(
        "--file",
        default="e:/Projetos/logtudo/Viagens_Extras/COT D2D 24-06.xlsx",
        help="Path to the Excel file",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without committing to the database",
    )
    args = parser.parse_args()

    run_update(args.file, dry_run=args.dry_run)
