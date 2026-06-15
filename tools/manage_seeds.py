from pathlib import Path
import sys

# ensure project root is importable when running this script from tools/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import engine
from app.seeds.seed_tracker import get_applied_seeds, init_seed_tracker


def show_seed_history():
    """Mostra o histórico de seeds aplicados."""
    with engine.connect() as conn:
        init_seed_tracker(conn)
        seeds = get_applied_seeds(conn)
        
        if not seeds:
            print("❌ Nenhum seed foi aplicado ainda.")
            return
        
        print("\n📋 Histórico de Seeds Aplicados:")
        print("─" * 70)
        for seed_name, version, applied_at in seeds:
            print(f"  • {seed_name} (v{version})")
            print(f"    Aplicado em: {applied_at}")
        print("─" * 70)
        print(f"Total: {len(seeds)} seeds\n")


def reset_seed(seed_name: str):
    """Remove um seed do rastreamento para permitir re-aplicação."""
    from sqlalchemy import text
    
    with engine.begin() as conn:
        result = conn.execute(text("""
            DELETE FROM seed_version WHERE seed_name = :name
        """), {"name": seed_name})
        
        if result.rowcount > 0:
            print(f"✅ Seed '{seed_name}' foi resetado.")
            print(f"   Na próxima execução, ele será re-aplicado.")
        else:
            print(f"❌ Seed '{seed_name}' não foi encontrado.")


def reset_all_seeds():
    """Remove todos os seeds do rastreamento."""
    from sqlalchemy import text
    
    with engine.begin() as conn:
        result = conn.execute(text("DELETE FROM seed_version"))
        print(f"✅ Todos os {result.rowcount} seeds foram resetados.")
        print(f"   Na próxima execução, todos serão re-aplicados.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python manage_seeds.py [command]")
        print("\nComandos disponíveis:")
        print("  show       - Mostra histórico de seeds aplicados")
        print("  reset NAME - Reseta um seed específico")
        print("  reset-all  - Reseta todos os seeds")
        sys.exit(1)
    
    command = sys.argv[1]
    
    if command == "show":
        show_seed_history()
    elif command == "reset" and len(sys.argv) > 2:
        reset_seed(sys.argv[2])
    elif command == "reset-all":
        reset_all_seeds()
    else:
        print(f"❌ Comando desconhecido: {command}")
        sys.exit(1)
