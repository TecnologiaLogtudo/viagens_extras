from __future__ import annotations
from pathlib import Path
from sqlmodel import Session
from app.db import engine, init_db
from app.services.fleet_import import import_fleet_from_excel


XLSX_PATH = Path("cadastro_veiculos_tratado.xlsx")


def migrate() -> None:
    init_db()
    with Session(engine) as session:
        summary = import_fleet_from_excel(session, XLSX_PATH)

    print(
        "Motoristas importados:",
        f"linhas={summary.rows_read}",
        f"bases_criadas={summary.bases_created}",
        f"motoristas_inseridos={summary.drivers_inserted}",
        f"motoristas_atualizados={summary.drivers_updated}",
        f"veiculos_inseridos={summary.vehicles_inserted}",
        f"veiculos_atualizados={summary.vehicles_updated}",
        f"linhas_ignoradas={summary.rows_skipped}",
        sep=" | ",
    )


if __name__ == "__main__":
    migrate()
