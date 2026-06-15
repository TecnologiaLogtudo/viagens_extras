from __future__ import annotations

from pathlib import Path
import sys

# ensure project root is importable when running this script from tools/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session
from app.db import engine, init_db_with_seeds
from app.services.bootstrap import seed_runtime_data, is_database_seeded


def run_seeds() -> None:
    """Executa a inicialização do banco de dados e carrega todos os seeds manualmente.

    Este utilitário é útil para popular o banco de dados principal (PostgreSQL/SQLite)
    com dados iniciais (bases operacionais, motoristas, veículos e usuários padrão).
    """
    print("🌾 Iniciando processo de migração e sementes manuais...")

    # Garante a inicialização das tabelas e seeds idempotentes da frota
    init_db_with_seeds()

    # Aplica catálogo de bases operacionais e usuários padrão
    with Session(engine) as session:
        if not is_database_seeded(session):
            print("🌱 Populando catálogo de bases operacionais e usuários padrão...")
            seed_runtime_data(session)
            print("✅ Catálogo e usuários padrão criados com sucesso!")
        else:
            print("⏭️ Catálogo e usuários padrão já existem no banco. Pulando...")

    print("🚀 Processo de carga manual de seeds finalizado com sucesso!")


if __name__ == "__main__":
    run_seeds()
