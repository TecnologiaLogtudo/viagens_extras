"""
Script de teste para validar que os motoristas importados persistem
através de alterações no schema, migrations e recriações do banco.
"""
from sqlmodel import Session, select
from app.db import engine, init_db_with_seeds
from app.models import Driver, Vehicle, Base


def count_data():
    """Conta motoristas, veículos e bases no banco."""
    with Session(engine) as session:
        drivers = session.exec(select(Driver)).all()
        vehicles = session.exec(select(Vehicle)).all()
        bases = session.exec(select(Base)).all()
        return len(drivers), len(vehicles), len(bases)


def test_data_persistence():
    """
    Testa se os dados importados persistem após init_db_with_seeds.
    
    Scenario:
    1. Inicializa o banco com seeds (importa motoristas)
    2. Conta os motoristas
    3. Executa init_db_with_seeds novamente (simula migration/recriação)
    4. Verifica se os dados foram preservados
    """
    print("🧪 Teste de Persistência de Dados")
    print("=" * 70)
    
    # Step 1: Inicializar e importar
    print("\n1️⃣  Inicializando banco e importando dados...")
    init_db_with_seeds()
    drivers_count_1, vehicles_count_1, bases_count_1 = count_data()
    print(f"   ✓ Motoristas: {drivers_count_1}")
    print(f"   ✓ Veículos: {vehicles_count_1}")
    print(f"   ✓ Bases: {bases_count_1}")
    
    # Step 2: Simular migração/recriação executando init_db_with_seeds novamente
    print("\n2️⃣  Simulando migração/alteração do schema...")
    print("   (Executando init_db_with_seeds novamente)")
    init_db_with_seeds()
    drivers_count_2, vehicles_count_2, bases_count_2 = count_data()
    print(f"   ✓ Motoristas: {drivers_count_2}")
    print(f"   ✓ Veículos: {vehicles_count_2}")
    print(f"   ✓ Bases: {bases_count_2}")
    
    # Step 3: Validar que dados foram preservados
    print("\n3️⃣  Validando persistência...")
    if drivers_count_2 == drivers_count_1:
        print(f"   ✅ Motoristas: {drivers_count_2} == {drivers_count_1}")
    else:
        print(f"   ❌ Motoristas: {drivers_count_2} != {drivers_count_1}")
        
    if vehicles_count_2 == vehicles_count_1:
        print(f"   ✅ Veículos: {vehicles_count_2} == {vehicles_count_1}")
    else:
        print(f"   ❌ Veículos: {vehicles_count_2} != {vehicles_count_1}")
        
    if bases_count_2 == bases_count_1:
        print(f"   ✅ Bases: {bases_count_2} == {bases_count_1}")
    else:
        print(f"   ❌ Bases: {bases_count_2} != {bases_count_1}")
    
    # Step 4: Validar dados específicos
    print("\n4️⃣  Validando integridade de dados...")
    with Session(engine) as session:
        # Verificar se há motoristas com veículos
        drivers_with_vehicles = session.exec(
            select(Driver).where(Driver.vehicle_id.is_not(None))
        ).all()
        print(f"   ✓ Motoristas com veículo: {len(drivers_with_vehicles)}")
        
        # Verificar se há bases
        all_bases = session.exec(select(Base)).all()
        print(f"   ✓ Bases cadastradas: {len(all_bases)}")
        if all_bases:
            for base in all_bases[:3]:
                print(f"      - {base.name} ({base.location})")
    
    # Final verdict
    print("\n" + "=" * 70)
    if (drivers_count_2 == drivers_count_1 and 
        vehicles_count_2 == vehicles_count_1 and 
        bases_count_2 == bases_count_1):
        print("✅ TESTE PASSOU: Dados persistem através de migrations!")
    else:
        print("❌ TESTE FALHOU: Dados foram perdidos!")
    print("=" * 70)


if __name__ == "__main__":
    test_data_persistence()
