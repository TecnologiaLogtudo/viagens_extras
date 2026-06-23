import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# Ensure we can import from app
sys.path.append(os.getcwd())
load_dotenv()

from sqlmodel import Session, select
from app.db import engine
from app.models import User

# The new password hash identified from the SQLite database
new_password_hash = '4b1c6256afaf8d7b3c6a5ea730bac895'
email = "tecnologia@logtudo.com.br"

def update_password():
    print(f"Connecting to database: {os.getenv('DATABASE_URL')}")
    with Session(engine) as session:
        statement = select(User).where(User.email == email)
        user = session.exec(statement).first()
        
        if user:
            print(f"Usuário {email} encontrado. Atualizando senha...")
            user.password_hash = new_password_hash
            session.add(user)
            session.commit()
            print(f"Senha do usuário {email} atualizada com sucesso no PostgreSQL.")
        else:
            print(f"Usuário {email} não encontrado no banco de dados.")

if __name__ == "__main__":
    update_password()
