from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlmodel import Session, select

from app.auth import hash_password
from app.models import Base, Company, CompanyBase, User, UserCompanyBaseLink, UserRole


BASES_XLSX = Path("Bases operacionais.xlsx")

def is_database_seeded(session: Session) -> bool:
    """Return True when the database already has the expected baseline seed state.

    This prevents repeated startup seeding from altering existing production data
    after schema migrations or application restarts.
    """
    if session.exec(select(Company).limit(1)).first() is None:
        return False
    if session.exec(select(Base).limit(1)).first() is None:
        return False
    if session.exec(select(User).where(User.email == "gerente@logtudo.local")).first() is None:
        return False
    return True


def _placeholder_cnpj(index: int) -> str:
    return f"00.000.000/0001-{index:02d}"


def seed_catalog_from_workbook(session: Session, bases_path: Path | str = BASES_XLSX) -> None:
    df = pd.read_excel(bases_path)
    company_names = list(dict.fromkeys(df["Empresa"].astype(str).str.strip()))

    companies: dict[str, Company] = {}
    for index, company_name in enumerate(company_names, start=1):
        company = session.exec(select(Company).where(Company.name == company_name)).first()
        if not company:
            company = Company(name=company_name, cnpj=_placeholder_cnpj(index))
            session.add(company)
            session.flush()
        companies[company_name] = company

    bases: dict[tuple[str, str], Base] = {}
    for _, row in (
        df[["Estado", "Cidade", "SLA"]]
        .assign(
            Estado=df["Estado"].astype(str).str.strip(),
            Cidade=df["Cidade"].astype(str).str.strip(),
            SLA=df["SLA"].fillna(30).astype(int),
        )
        .drop_duplicates(subset=["Estado", "Cidade", "SLA"])
        .iterrows()
    ):
        state = str(row["Estado"]).strip()
        city = str(row["Cidade"]).strip()
        sla_minutes = int(row["SLA"])
        base = session.exec(select(Base).where(Base.name == state, Base.location == city)).first()
        if not base:
            base = Base(
                name=state,
                location=city,
                sla_minutes=sla_minutes,
                min_advance_minutes=120,
                active=True,
            )
            session.add(base)
            session.flush()
        bases[(state, city)] = base

    seen_links: set[tuple[int, int]] = set()
    for _, row in df.iterrows():
        company = companies[str(row["Empresa"]).strip()]
        base = bases[(str(row["Estado"]).strip(), str(row["Cidade"]).strip())]
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
                    contract_sla_minutes=int(row["SLA"]) if pd.notna(row["SLA"]) else 30,
                )
            )
        seen_links.add(link_key)

    session.commit()


def migrate_supervisor_company_base_links(session: Session) -> None:
    supervisors = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).all()
    for supervisor in supervisors:
        legacy_base_ids = [base.id for base in supervisor.bases if base.id is not None]
        if supervisor.base_id is not None and supervisor.base_id not in legacy_base_ids:
            legacy_base_ids.append(supervisor.base_id)

        for base_id in legacy_base_ids:
            company_base_links = session.exec(select(CompanyBase).where(CompanyBase.base_id == base_id)).all()
            if len(company_base_links) != 1:
                continue
            company_base = company_base_links[0]
            existing_link = session.exec(
                select(UserCompanyBaseLink).where(
                    UserCompanyBaseLink.user_id == supervisor.id,
                    UserCompanyBaseLink.company_base_id == company_base.id,
                )
            ).first()
            if not existing_link:
                session.add(UserCompanyBaseLink(user_id=supervisor.id, company_base_id=company_base.id))

    session.commit()


def seed_default_users(session: Session) -> None:
    # Manager
    if not session.exec(select(User).where(User.email == "gerente@logtudo.local")).first():
        session.add(
            User(
                full_name="Gerente Logtudo",
                email="gerente@logtudo.local",
                role=UserRole.LOGISTICS_MANAGER,
                company_name="Logtudo",
                password_hash=hash_password("gerente123"),
                is_active=True,
            )
        )

    # Partner (requires a company)
    if not session.exec(select(User).where(User.email == "parceiro@logtudo.local")).first():
        company = session.exec(select(Company).order_by(Company.id)).first()
        if company:
            session.add(
                User(
                    full_name="Parceiro Teste",
                    email="parceiro@logtudo.local",
                    role=UserRole.PARTNER_REQUESTER,
                    company_id=company.id,
                    company_name=company.name,
                    password_hash=hash_password("parceiro123"),
                    is_active=True,
                )
            )

    # Supervisor (requires a base)
    if not session.exec(select(User).where(User.email == "supervisor@logtudo.local")).first():
        # Prefer BA - Salvador where the spreadsheet fleet is loaded
        base = session.exec(select(Base).where(Base.name == "BA", Base.location == "Salvador")).first()
        if not base:
            base = session.exec(select(Base).order_by(Base.id)).first()
        if base:
            supervisor = User(
                full_name="Supervisor Teste",
                email="supervisor@logtudo.local",
                role=UserRole.BASE_SUPERVISOR,
                base_id=base.id,
                company_name="Logtudo",
                password_hash=hash_password("supervisor123"),
                is_active=True,
            )
            session.add(supervisor)
            session.flush()

            # Automatically create UserCompanyBaseLink for the supervisor's base
            company_bases = session.exec(select(CompanyBase).where(CompanyBase.base_id == base.id)).all()
            for cb in company_bases:
                session.add(UserCompanyBaseLink(user_id=supervisor.id, company_base_id=cb.id))

    # Finance
    if not session.exec(select(User).where(User.email == "financeiro@logtudo.local")).first():
        session.add(
            User(
                full_name="Financeiro Logtudo",
                email="financeiro@logtudo.local",
                role=UserRole.FINANCE_READONLY,
                company_name="Logtudo",
                password_hash=hash_password("financeiro123"),
                is_active=True,
            )
        )

    session.commit()


def seed_runtime_data(session: Session) -> None:
    seed_catalog_from_workbook(session)
    seed_default_users(session)
    migrate_supervisor_company_base_links(session)
