"""Seeds do banco de dados."""
from sqlmodel import Session
from sqlalchemy.engine import Connection

from .seed_tracker import init_seed_tracker
from .seed_fleet import seed_fleet_import


def apply_all_seeds(session: Session, connection: Connection) -> None:
    """Aplica todos os seeds em ordem."""
    print("\n🌾 Iniciando processo de seeds...")
    
    # Inicializar rastreador de seeds
    init_seed_tracker(connection)
    
    # Aplicar seeds
    seed_fleet_import(session, connection)
    
    print("🌾 Processo de seeds finalizado!\n")
