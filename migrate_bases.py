import pandas as pd
from sqlmodel import Session, select, delete
from app.db import engine
from app.models import Base, Company, CompanyBase

def migrate():
    # Load Excel
    df = pd.read_excel('Bases operacionais.xlsx')

    with Session(engine) as session:
        # Delete existing bases and company-base links
        session.exec(delete(CompanyBase))
        session.exec(delete(Base))
        session.commit()

        # Iterate over the dataframe
        for _, row in df.iterrows():
            empresa_nome = row['Empresa']
            estado = row['Estado']
            cidade = row['Cidade']
            sla = row['SLA']

            # Find or create Company
            company = session.exec(select(Company).where(Company.name == empresa_nome)).first()
            if not company:
                company = Company(name=empresa_nome, cnpj="00.000.000/0000-00")
                session.add(company)
                session.commit()
                session.refresh(company)

            # Find or create Base
            base = session.exec(select(Base).where(Base.name == estado, Base.location == cidade)).first()
            if not base:
                base = Base(name=estado, location=cidade, sla_minutes=int(sla))
                session.add(base)
                session.commit()
                session.refresh(base)

            # Find or create CompanyBase
            company_base = session.exec(
                select(CompanyBase).where(
                    CompanyBase.company_id == company.id,
                    CompanyBase.base_id == base.id
                )
            ).first()
            if not company_base:
                company_base = CompanyBase(
                    company_id=company.id,
                    base_id=base.id,
                    contract_sla_minutes=int(sla)
                )
                session.add(company_base)
                session.commit()

if __name__ == "__main__":
    migrate()
