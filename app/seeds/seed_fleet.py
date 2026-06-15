"""
Seed para importação de frota de motoristas e veículos.
Este seed é idempotente e rastreado pelo seed_tracker.
"""
from pathlib import Path
from sqlmodel import Session, select
from app.models import Base, Driver, Vehicle
from app.services.fleet_import import import_fleet_from_excel
from .seed_tracker import is_seed_applied, mark_seed_applied

SEED_NAME = "001_import_fleet"
SEED_VERSION = 1


def seed_fleet_import(session: Session, connection) -> bool:
    """
    Importa frota de motoristas e veículos do Excel.
    Retorna True se executado, False se já foi aplicado.
    """
    # Detect test environment to prevent heavy imports and side-effects during pytest
    import sys
    import os
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        print("⏭️  Ambiente de testes detectado, pulando seed de frota...")
        return False

    # Check if we have drivers in the database
    has_drivers = session.exec(select(Driver).limit(1)).first() is not None

    if is_seed_applied(connection, SEED_NAME, SEED_VERSION):
        if has_drivers:
            print(f"⏭️  Seed {SEED_NAME} v{SEED_VERSION} já foi aplicado e os dados existem, pulando...")
            return False
        else:
            print(f"⚠️  Seed {SEED_NAME} v{SEED_VERSION} já foi aplicado, mas a tabela de motoristas está vazia. Re-importando...")

    print(f"🌱 Aplicando seed {SEED_NAME} v{SEED_VERSION}...")

    # Caminho do arquivo Excel
    excel_file = Path(__file__).resolve().parent.parent.parent / "cadastro_veiculos_tratado.xlsx"

    if not excel_file.exists():
        print(f"⚠️  Arquivo de importação não encontrado: {excel_file}")
        return False

    # Executar importação
    summary = import_fleet_from_excel(session, excel_file)

    print(f"📊 Resultado da importação:")
    print(f"   - Linhas lidas: {summary.rows_read}")
    print(f"   - Bases criadas: {summary.bases_created}")
    print(f"   - Motoristas inseridos: {summary.drivers_inserted}")
    print(f"   - Motoristas atualizados: {summary.drivers_updated}")
    print(f"   - Veículos inseridos: {summary.vehicles_inserted}")
    print(f"   - Veículos atualizados: {summary.vehicles_updated}")
    print(f"   - Linhas ignoradas: {summary.rows_skipped}")

    # Marcar como aplicado
    mark_seed_applied(connection, SEED_NAME, SEED_VERSION)
    print(f"✅ Seed {SEED_NAME} v{SEED_VERSION} aplicado com sucesso!")

    return True
