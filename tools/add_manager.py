import sys
import os
from pathlib import Path

# Add project root to sys.path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from sqlmodel import Session, select
from app.db import engine, init_db
from app.models import User, UserRole
from app.auth import hash_password

def add_manager():
    # We will use the default engine configuration from app.db
    # If DATABASE_URL is in environment, it uses it, otherwise SQLite mvp.db
    
    # Optional: init_db() to ensure tables exist
    init_db()
    
    email = "tecnologia@logtudo.com.br"
    password = "teste123"
    full_name = "Gerente Tecnologia"
    
    with Session(engine) as session:
        # Check if user already exists
        statement = select(User).where(User.email == email)
        user = session.exec(statement).first()
        
        if user:
            print(f"Usuário {email} já existe. Atualizando senha e papel...")
            user.password_hash = hash_password(password)
            user.role = UserRole.LOGISTICS_MANAGER
            user.full_name = full_name
            user.is_active = True
        else:
            print(f"Criando novo gerente: {email}")
            user = User(
                full_name=full_name,
                email=email,
                role=UserRole.LOGISTICS_MANAGER,
                company_name="Logtudo",
                password_hash=hash_password(password),
                is_active=True
            )
            session.add(user)
        
        session.commit()
        print(f"Gerente {email} configurado com sucesso!")

if __name__ == "__main__":
    add_manager()
