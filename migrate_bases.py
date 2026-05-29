from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlmodel import Session, delete, select

from app.db import engine
from app.models import (
    Acceptance,
    Base,
    Company,
    CompanyBase,
    Dispatch,
    Document,
    Driver,
    EventLog,
    Notification,
    OTPChallenge,
    OperationalConfirmation,
    TravelRequest,
    User,
    UserBaseLink,
    UserRole,
    Vehicle,
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
        # Clear dependent operational data first so the base/company reset is clean.
        for model in (
            OperationalConfirmation,
            Acceptance,
            Dispatch,
            OTPChallenge,
            Document,
            Notification,
            EventLog,
            TravelRequest,
            Driver,
            Vehicle,
            UserBaseLink,
            CompanyBase,
            Company,
            Base,
        ):
            session.exec(delete(model))

        session.flush()

        companies: dict[str, Company] = {}
        for index, company_name in enumerate(company_order, start=1):
            company = Company(name=company_name, cnpj=_placeholder_cnpj(index))
            session.add(company)
            session.flush()
            companies[company_name] = company

        bases: dict[tuple[str, str], Base] = {}
        for row in base_rows:
            base = Base(
                name=row["Estado"],
                location=row["Cidade"],
                sla_minutes=int(row["SLA"]),
                min_advance_minutes=120,
                active=True,
            )
            session.add(base)
            session.flush()
            bases[(row["Estado"], row["Cidade"])] = base

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
            session.add(
                CompanyBase(
                    company_id=company.id,
                    base_id=base.id,
                    contract_sla_minutes=sla_minutes,
                )
            )
            seen_links.add(link_key)

        partner = session.exec(select(User).where(User.email == "parceiro@logtudo.local")).first()
        if partner and company_order:
            first_company = companies[company_order[0]]
            partner.company_id = first_company.id
            partner.company_name = first_company.name
            partner.role = UserRole.PARTNER_REQUESTER
            session.add(partner)

        supervisor = session.exec(select(User).where(User.email == "supervisor@logtudo.local")).first()
        if supervisor and base_rows:
            first_base = bases[(base_rows[0]["Estado"], base_rows[0]["Cidade"])]
            supervisor.base_id = first_base.id
            supervisor.company_name = "Logtudo"
            supervisor.role = UserRole.BASE_SUPERVISOR
            session.add(supervisor)
            session.exec(delete(UserBaseLink).where(UserBaseLink.user_id == supervisor.id))
            session.add(UserBaseLink(user_id=supervisor.id, base_id=first_base.id))

        session.commit()


if __name__ == "__main__":
    migrate()
