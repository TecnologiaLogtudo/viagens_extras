from pathlib import Path
import sys

# ensure project root is importable when running this script from tools/
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlmodel import Session, delete, or_
from app.db import engine
from app.models import (
    Company,
    CompanyBase,
    User,
    UserRole,
    UserBaseLink
)

def delete_companies_and_supervisors():
    print("ATENÇÃO: Este script irá deletar TODAS as empresas, parceiros e supervisores.")
    print("DICA: Certifique-se de não haver solicitações ativas dependentes ou rode 'delete_all_requests.py' antes para evitar erros de chave estrangeira.")
    confirm = input("Tem certeza que deseja continuar? (s/N): ")
    
    if confirm.lower() != "s":
        print("Operação cancelada.")
        return

    with Session(engine) as session:
        try:
            print("Deletando vínculos com bases...")
            session.execute(delete(CompanyBase))
            session.execute(delete(UserBaseLink))
            
            print("Deletando usuários (supervisores e parceiros)...")
            session.execute(delete(User).where(or_(User.role == UserRole.BASE_SUPERVISOR, User.role == UserRole.PARTNER_REQUESTER)))
            
            print("Deletando empresas...")
            session.execute(delete(Company))
            
            session.commit()
            print("Sucesso: Cadastros de empresas e supervisores foram deletados.")
        except Exception as e:
            session.rollback()
            print(f"Erro ao deletar os cadastros: {e}")

if __name__ == "__main__":
    delete_companies_and_supervisors()
