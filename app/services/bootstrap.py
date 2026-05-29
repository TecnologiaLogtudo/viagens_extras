from __future__ import annotations

from pathlib import Path

import pandas as pd
from sqlmodel import Session, select

from app.auth import hash_password
from app.models import Base, Company, CompanyBase, User, UserBaseLink, UserRole


BASES_XLSX = Path("Bases operacionais.xlsx")


def _placeholder_cnpj(index: int) -> str:
    return f"00.000.000/0001-{index:02d}"


def _default_password(role: UserRole) -> str:
    return {
        UserRole.PARTNER_REQUESTER: "parceiro123",
        UserRole.BASE_SUPERVISOR: "supervisor123",
        UserRole.LOGISTICS_MANAGER: "gerente123",
        UserRole.FINANCE_READONLY: "financeiro123",
    }.get(role, "logtudo123")


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
        else:
            base.sla_minutes = sla_minutes
            base.min_advance_minutes = 120
            base.active = True
            session.add(base)
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
        else:
            existing_link.contract_sla_minutes = int(row["SLA"]) if pd.notna(row["SLA"]) else 30
            session.add(existing_link)
        seen_links.add(link_key)

    session.flush()

    first_company_name = company_names[0] if company_names else None
    first_base = next(iter(bases.values()), None)

    partner = session.exec(select(User).where(User.email == "parceiro@logtudo.local")).first()
    if first_company_name:
        company = companies[first_company_name]
        if not partner:
            partner = User(
                full_name="Ana Parceira",
                email="parceiro@logtudo.local",
                role=UserRole.PARTNER_REQUESTER,
                company_name=company.name,
                company_id=company.id,
                password_hash=hash_password(_default_password(UserRole.PARTNER_REQUESTER)),
            )
            session.add(partner)
        else:
            partner.company_id = company.id
            partner.company_name = company.name
            partner.role = UserRole.PARTNER_REQUESTER
            if not partner.password_hash:
                partner.password_hash = hash_password(_default_password(UserRole.PARTNER_REQUESTER))
            session.add(partner)

    supervisor = session.exec(select(User).where(User.email == "supervisor@logtudo.local")).first()
    if first_base:
        if not supervisor:
            supervisor = User(
                full_name="Bruno Supervisor",
                email="supervisor@logtudo.local",
                role=UserRole.BASE_SUPERVISOR,
                company_name="Logtudo",
                base_id=first_base.id,
                password_hash=hash_password(_default_password(UserRole.BASE_SUPERVISOR)),
            )
            session.add(supervisor)
            session.flush()
        else:
            supervisor.role = UserRole.BASE_SUPERVISOR
            supervisor.company_name = "Logtudo"
            supervisor.base_id = first_base.id
            if not supervisor.password_hash:
                supervisor.password_hash = hash_password(_default_password(UserRole.BASE_SUPERVISOR))
            session.add(supervisor)
        existing_link = session.exec(
            select(UserBaseLink).where(
                UserBaseLink.user_id == supervisor.id,
                UserBaseLink.base_id == first_base.id,
            )
        ).first()
        if not existing_link:
            session.add(UserBaseLink(user_id=supervisor.id, base_id=first_base.id))

    for email, role, full_name, company_name in (
        ("gerente@logtudo.local", UserRole.LOGISTICS_MANAGER, "Carla Gerente", "Logtudo"),
        ("financeiro@logtudo.local", UserRole.FINANCE_READONLY, "Diego Financeiro", "Logtudo"),
    ):
        user = session.exec(select(User).where(User.email == email)).first()
        if not user:
            session.add(
                User(
                    full_name=full_name,
                    email=email,
                    role=role,
                    company_name=company_name,
                    password_hash=hash_password(_default_password(role)),
                )
            )
        else:
            user.role = role
            user.company_name = company_name
            if not user.password_hash:
                user.password_hash = hash_password(_default_password(role))
            session.add(user)

    session.commit()


def seed_runtime_data(session: Session) -> None:
    seed_catalog_from_workbook(session)
