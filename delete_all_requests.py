import sys
from sqlmodel import Session, select, delete
from app.db import engine
from app.models import (
    TravelRequest, 
    OperationalConfirmation, 
    Acceptance, 
    Dispatch, 
    OTPChallenge, 
    Document, 
    Notification, 
    EventLog
)

def delete_all_requests():
    print("ATENCAO: Este script ira deletar TODAS as solicitacoes e dados vinculados.")
    confirm = input("Tem certeza que deseja continuar? (s/N): ")
    
    if confirm.lower() != "s":
        print("Operacao cancelada.")
        return

    with Session(engine) as session:
        try:
            print("Limpando tabelas vinculadas...")
            session.execute(delete(OperationalConfirmation))
            session.execute(delete(Acceptance))
            session.execute(delete(Dispatch))
            session.execute(delete(OTPChallenge))
            session.execute(delete(Document))
            session.execute(delete(Notification))
            session.execute(delete(EventLog))
            
            print("Limpando solicitacoes de viagem...")
            session.execute(delete(TravelRequest))
            
            session.commit()
            print("Sucesso: Todas as solicitacoes foram deletadas.")
        except Exception as e:
            session.rollback()
            print(f"Erro ao deletar solicitacoes: {e}")

if __name__ == "__main__":
    delete_all_requests()
