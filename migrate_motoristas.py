from __future__ import annotations
from pathlib import Path
from sqlmodel import Session
from app.db import engine, init_db_with_seeds


def migrate() -> None:
    init_db_with_seeds()
    print("✅ Importação de motoristas completada!")


if __name__ == "__main__":
    migrate()
