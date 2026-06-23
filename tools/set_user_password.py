import sys
import os
from pathlib import Path

# Add project root to sys.path to allow imports from app
sys.path.append(str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from sqlmodel import Session, select
from app.db import engine, init_db
from app.models import User
from app.auth import hash_password

def set_password(email: str, password_plain: str):
    email = email.strip().lower()
    password_plain = password_plain.strip()
    
    if not email or not password_plain:
        print("Erro: O e-mail e a senha não podem ser vazios.")
        sys.exit(1)
        
    print(f"Conectando ao banco de dados: {os.getenv('DATABASE_URL', 'SQLite padrão')}")
    
    with Session(engine) as session:
        statement = select(User).where(User.email == email)
        user = session.exec(statement).first()
        
        if not user:
            print(f"Erro: Usuário '{email}' não encontrado no banco de dados.")
            sys.exit(1)
            
        print(f"Usuário '{email}' encontrado. Criptografando senha...")
        user.password_hash = hash_password(password_plain)
        session.add(user)
        session.commit()
        print(f"Sucesso! A senha de '{email}' foi atualizada e salva com hash seguro.")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python tools/set_user_password.py <email> <nova_senha>")
        sys.exit(1)
        
    email_arg = sys.argv[1]
    password_arg = sys.argv[2]
    set_password(email_arg, password_arg)
