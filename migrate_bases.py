from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlmodel import Session, select

from app.db import engine
from app.models import (
    Base,
    Company,
    CompanyBase,
    User,
    UserBaseLink,
    UserRole,
)


XLSX_PATH = Path("Bases operacionais.xlsx")


def _placeholder_cnpj(index: int) -> str:
    return f"00.000.000/0001-{index:02d}"


def migrate() -> None:
    df = pd.read_excel(XLSX_PATH)

    company_order = list(dict.fromkeys(df["Empresa"].astype(str).str.strip()))
    base_rows = (
        df[["Estado", "Cidade", "SLA"]]
        .assign(
            Estado=df["Estado"].astype(str).str.strip(),
            Cidade=df["Cidade"].astype(str).str.strip(),
            SLA=df["SLA"].fillna(30).astype(int),
        )
        .drop_duplicates(subset=["Estado", "Cidade", "SLA"])
        .to_dict("records")
    )

    with Session(engine) as session:
        companies: dict[str, Company] = {}
        for index, company_name in enumerate(company_order, start=1):
            company = session.exec(select(Company).where(Company.name == company_name)).first()
            if not company:
                company = Company(name=company_name, cnpj=_placeholder_cnpj(index))
                session.add(company)
                session.flush()
            companies[company_name] = company

        bases: dict[tuple[str, str], Base] = {}
        for row in base_rows:
            state = str(row["Estado"]).strip()
            city = str(row["Cidade"]).strip()
            base = session.exec(select(Base).where(Base.name == state, Base.location == city)).first()
            if not base:
                base = Base(
                    name=state,
                    location=city,
                    sla_minutes=int(row["SLA"]),
                    min_advance_minutes=120,
                    active=True,
                )
                session.add(base)
                session.flush()
            bases[(state, city)] = base

        seen_links: set[tuple[int, int]] = set()
        for _, row in df.iterrows():
            company_name = str(row["Empresa"]).strip()
            state = str(row["Estado"]).strip()
            city = str(row["Cidade"]).strip()
            sla_minutes = int(row["SLA"]) if pd.notna(row["SLA"]) else 30

            company = companies[company_name]
            base = bases[(state, city)]
            link_key = (company.id, base.id)
            if link_key in seen_links:
                continue
            
            existing_link = session.exec(
                select(CompanyBase).where(
                    CompanyBase.company_id == company.id,
                    CompanyBase.base_id == base.id,
                )
            ).first()
            if not existing_link:
                session.add(
                    CompanyBase(
                        company_id=company.id,
                        base_id=base.id,
                        contract_sla_minutes=sla_minutes,
                    )
                )
            seen_links.add(link_key)

        partner = session.exec(select(User).where(User.email == "parceiro@logtudo.local")).first()
        if partner and company_order and partner.company_id is None:
            first_company = companies[company_order[0]]
            partner.company_id = first_company.id
            partner.company_name = first_company.name
            partner.role = UserRole.PARTNER_REQUESTER
            session.add(partner)

        supervisor = session.exec(select(User).where(User.email == "supervisor@logtudo.local")).first()
        if supervisor and base_rows and supervisor.base_id is None:
            first_base = bases[(base_rows[0]["Estado"], base_rows[0]["Cidade"])]
            supervisor.base_id = first_base.id
            supervisor.company_name = "Logtudo"
            supervisor.role = UserRole.BASE_SUPERVISOR
            session.add(supervisor)
            
            existing_user_base_link = session.exec(
                select(UserBaseLink).where(
                    UserBaseLink.user_id == supervisor.id,
                    UserBaseLink.base_id == first_base.id
                )
            ).first()
            if not existing_user_base_link:
                session.add(UserBaseLink(user_id=supervisor.id, base_id=first_base.id))

        session.commit()


if __name__ == "__main__":
    migrate()
