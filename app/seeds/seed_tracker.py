"""
Rastreador de seeds para garantir que dados importados não sejam perdidos
com alterações no banco, migrations ou recriações de schema.
"""
from datetime import datetime
from pathlib import Path
from sqlalchemy import text
from sqlmodel import Session


SEEDS_DIR = Path(__file__).resolve().parent
SEED_VERSION_TABLE = "seed_version"


def init_seed_tracker(connection) -> None:
    """Cria a tabela de rastreamento de seeds se não existir."""
    dialect_name = connection.dialect.name
    if dialect_name == "sqlite":
        id_col = "id INTEGER PRIMARY KEY AUTOINCREMENT"
        text_type = "TEXT"
    else:
        id_col = "id SERIAL PRIMARY KEY"
        text_type = "VARCHAR(255)"

    connection.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {SEED_VERSION_TABLE} (
            {id_col},
            seed_name {text_type} UNIQUE NOT NULL,
            version INTEGER NOT NULL,
            applied_at {text_type} NOT NULL,
            checksum {text_type}
        )
    """))
    connection.commit()


def is_seed_applied(connection, seed_name: str, version: int) -> bool:
    """Verifica se um seed foi aplicado."""
    result = connection.execute(text(f"""
        SELECT COUNT(*) FROM {SEED_VERSION_TABLE}
        WHERE seed_name = :name AND version >= :version
    """), {"name": seed_name, "version": version}).scalar()
    return result > 0


def mark_seed_applied(connection, seed_name: str, version: int, checksum: str = None) -> None:
    """Marca um seed como aplicado."""
    dialect_name = connection.dialect.name
    if dialect_name == "sqlite":
        connection.execute(text(f"""
            INSERT OR REPLACE INTO {SEED_VERSION_TABLE} (seed_name, version, applied_at, checksum)
            VALUES (:name, :version, :applied_at, :checksum)
        """), {
            "name": seed_name,
            "version": version,
            "applied_at": datetime.utcnow().isoformat(),
            "checksum": checksum
        })
    else:
        connection.execute(text(f"""
            INSERT INTO {SEED_VERSION_TABLE} (seed_name, version, applied_at, checksum)
            VALUES (:name, :version, :applied_at, :checksum)
            ON CONFLICT (seed_name) DO UPDATE SET
                version = EXCLUDED.version,
                applied_at = EXCLUDED.applied_at,
                checksum = EXCLUDED.checksum
        """), {
            "name": seed_name,
            "version": version,
            "applied_at": datetime.utcnow().isoformat(),
            "checksum": checksum
        })
    connection.commit()


def get_applied_seeds(connection) -> list:
    """Lista todos os seeds aplicados."""
    result = connection.execute(text(f"""
        SELECT seed_name, version, applied_at FROM {SEED_VERSION_TABLE}
        ORDER BY applied_at
    """)).fetchall()
    return result
