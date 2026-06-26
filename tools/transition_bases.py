"""
Migration script to merge generic bases into specific location bases.

This script updates all driver, vehicle, user, travel request, and company
base records pointing to generic bases (e.g. 'SE - SE') to point to the
specific target base (e.g. 'SE - Aracaju'), and then deletes the generic base.
"""

import argparse
import os
import sys
import unicodedata
from sqlmodel import Session, select, delete

# Ensure project root is in sys.path
sys.path.insert(0, os.getcwd())

from app.db import engine
from app.models import Base, Driver, Vehicle, User, UserBaseLink, TravelRequest, CompanyBase

# Mappings of (Source State, Source Location) -> (Target State, Target Location)
TRANSITIONS = [
    ("SE", "SE", "SE", "Aracaju"),
    ("AL", "AL", "AL", "Rio Largo"),
    ("BA", "BA", "BA", "Salvador"),
    ("CE", "CE", "CE", "Fortaleza"),
    ("ES", "ES", "ES", "Vitória"),
    ("MA", "MA", "MA", "São Luis"),
    ("MG", "MG", "MG", "Confins"),
    ("PA", "PA", "PA", "Santarém"),
    ("PB", "PB", "PB", "Bayeux"),
    ("PE", "PE", "PE", "Cabo de Santo Agostinho"),
    ("PI", "PI", "PI", "Teresina"),
    ("PR", "PR", "PR", "Foz do Iguaçu"),
    ("RN", "RN", "RN", "Natal"),
    ("RS", "RS", "RS", "Porto Alegre"),
    ("SP", "SP", "SP", "São José dos Campos"),
]


def normalize_str(value: str) -> str:
    """Normalize string: uppercase, remove accents, and strip spaces."""
    if not value:
        return ""
    value = value.strip().upper()
    normalized = "".join(
        c for c in unicodedata.normalize("NFD", value)
        if unicodedata.category(c) != "Mn"
    )
    return " ".join(normalized.split())


def migrate_base(session: Session, src_base: Base, tgt_base: Base):
    """Migrate all references from src_base to tgt_base and delete src_base."""
    src_id = src_base.id
    tgt_id = tgt_base.id

    # 1. Update Drivers
    drivers = session.exec(select(Driver).where(Driver.base_id == src_id)).all()
    for driver in drivers:
        driver.base_id = tgt_id
        session.add(driver)
    print(f"  - Moved {len(drivers)} drivers.")

    # 2. Update Vehicles
    vehicles = session.exec(select(Vehicle).where(Vehicle.base_id == src_id)).all()
    for vehicle in vehicles:
        vehicle.base_id = tgt_id
        session.add(vehicle)
    print(f"  - Moved {len(vehicles)} vehicles.")

    # 3. Update Users
    users = session.exec(select(User).where(User.base_id == src_id)).all()
    for user in users:
        user.base_id = tgt_id
        session.add(user)
    print(f"  - Moved {len(users)} users.")

    # 4. Update Travel Requests
    requests = session.exec(select(TravelRequest).where(TravelRequest.base_id == src_id)).all()
    for req in requests:
        req.base_id = tgt_id
        session.add(req)
    print(f"  - Moved {len(requests)} travel requests.")

    # 5. Update Company Bases
    company_bases = session.exec(select(CompanyBase).where(CompanyBase.base_id == src_id)).all()
    for cb in company_bases:
        cb.base_id = tgt_id
        session.add(cb)
    print(f"  - Moved {len(company_bases)} company bases.")

    # 6. Migrate UserBaseLink relationships
    links = session.exec(select(UserBaseLink).where(UserBaseLink.base_id == src_id)).all()
    moved_links = 0
    deleted_links = 0
    for link in links:
        # Check if user is already linked to the target base
        existing_link = session.exec(
            select(UserBaseLink).where(
                UserBaseLink.user_id == link.user_id,
                UserBaseLink.base_id == tgt_id
            )
        ).first()

        if existing_link:
            # Delete redundant link to source base
            session.delete(link)
            deleted_links += 1
        else:
            # Update base_id directly since UserBaseLink has compound primary key
            # SQLAlchemy doesn't support direct primary key mutation easily,
            # so we delete the old record and insert the new one to be safe.
            user_id = link.user_id
            session.delete(link)
            new_link = UserBaseLink(user_id=user_id, base_id=tgt_id)
            session.add(new_link)
            moved_links += 1

    print(f"  - UserBaseLink: {moved_links} moved, {deleted_links} redundant deleted.")

    # Flush changes to resolve constraint checks before deletion
    session.flush()

    # 7. Delete the old Base record
    session.delete(src_base)
    print(f"  - Deleted old base ID {src_id} ('{src_base.name} - {src_base.location}').")


def run_transition(dry_run: bool = False):
    """Find eligible bases and perform the migration."""
    print("Starting Base Transition Migration...")
    with Session(engine) as session:
        db_bases = session.exec(select(Base)).all()

        transitions_performed = 0

        for src_state, src_loc, tgt_state, tgt_loc in TRANSITIONS:
            # Identify source base using possible variations
            possible_src_keys = [
                (src_state, src_loc),
                (src_state, f"{src_state} - {src_state}"),
                (f"{src_state} - {src_state}", f"{src_state} - {src_state}"),
                (src_state, src_state.lower()),
                (src_state, src_state.upper())
            ]

            src_base = None
            for name, loc in possible_src_keys:
                for b in db_bases:
                    if normalize_str(b.name) == normalize_str(name) and normalize_str(b.location) == normalize_str(loc):
                        src_base = b
                        break
                if src_base:
                    break

            # Identify target base
            tgt_base = None
            for b in db_bases:
                if normalize_str(b.name) == normalize_str(tgt_state) and normalize_str(b.location) == normalize_str(tgt_loc):
                    tgt_base = b
                    break

            if src_base and tgt_base:
                print(
                    f"\nMigrating base: '{src_base.name} - {src_base.location}' (ID {src_base.id}) "
                    f"-> '{tgt_base.name} - {tgt_base.location}' (ID {tgt_base.id})..."
                )
                migrate_base(session, src_base, tgt_base)
                transitions_performed += 1
            elif src_base and not tgt_base:
                print(
                    f"\n[WARNING] Source base '{src_base.name} - {src_base.location}' found, "
                    f"but target base '{tgt_state} - {tgt_loc}' was not found in DB. Skipping."
                )

        if transitions_performed == 0:
            print("\nNo bases matching the transition criteria were found in the database.")
            print("This is normal if the database was seeded cleanly without generic bases.")

        if dry_run:
            print("\n[DRY RUN] Rolling back transitions.")
            session.rollback()
        else:
            session.commit()
            print("\nSuccessfully updated database!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Transition generic bases to target bases.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run without committing to the database",
    )
    args = parser.parse_args()

    run_transition(dry_run=args.dry_run)
