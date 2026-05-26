from fastapi.testclient import TestClient
import pytest
from datetime import datetime, timezone
from sqlmodel import Session, select

from app.db import engine
from app.main import app
from app.models import Base, Company, CompanyBase, DecisionType, OperationalConfirmation, RequestStatus, UserBaseLink, TravelRequest, User
from app.services.workflow import DomainError


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, email: str, password: str):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_login_creates_session_and_redirects_by_role(client: TestClient):
    resp = login(client, "parceiro@logtudo.local", "parceiro123")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/partner"


def test_logout_invalidates_session(client: TestClient):
    login(client, "parceiro@logtudo.local", "parceiro123")
    out = client.post("/logout", follow_redirects=False)
    assert out.status_code == 303
    assert out.headers["location"] == "/login"

    private = client.get("/partner", follow_redirects=False)
    assert private.status_code == 303
    assert private.headers["location"] == "/login"


def test_requires_login_for_partner_and_company_areas(client: TestClient):
    client.post("/logout")
    r1 = client.get("/partner", follow_redirects=False)
    r2 = client.get("/empresa/operacoes", follow_redirects=False)

    assert r1.status_code == 303 and r1.headers["location"] == "/login"
    assert r2.status_code == 303 and r2.headers["location"] == "/login"


def test_partner_cannot_access_company_area(client: TestClient):
    login(client, "parceiro@logtudo.local", "parceiro123")
    resp = client.get("/empresa/operacoes")
    assert resp.status_code == 403


def test_finance_cannot_access_operations(client: TestClient):
    login(client, "financeiro@logtudo.local", "financeiro123")
    resp = client.get("/empresa/operacoes")
    assert resp.status_code == 403


def test_supervisor_cannot_access_finance(client: TestClient):
    login(client, "supervisor@logtudo.local", "supervisor123")
    resp = client.get("/empresa/financeiro")
    assert resp.status_code == 403


def test_manager_can_access_consolidated_views(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")
    op = client.get("/empresa/operacoes")
    fin = client.get("/empresa/financeiro")
    mgr = client.get("/empresa/gerencial")

    assert op.status_code == 200
    assert fin.status_code == 200
    assert mgr.status_code == 200


def test_top_navigation_shows_only_allowed_links(client: TestClient):
    login(client, "parceiro@logtudo.local", "parceiro123")
    partner_page = client.get("/partner")
    assert "Portal Parceiro" in partner_page.text
    assert "Operações" not in partner_page.text
    assert "Financeiro" not in partner_page.text

    login(client, "gerente@logtudo.local", "gerente123")
    manager_page = client.get("/empresa/gerencial")
    assert "Operações" in manager_page.text
    assert "Financeiro" in manager_page.text
    assert "Gerencial" in manager_page.text
    assert "user_id=" not in manager_page.text


def test_partner_otp_send_failure_shows_friendly_message(client: TestClient, monkeypatch):
    login(client, "parceiro@logtudo.local", "parceiro123")

    with Session(engine) as session:
        partner = session.exec(select(User).where(User.email == "parceiro@logtudo.local")).first()
        base = session.exec(select(Base)).first()
        company = session.get(Company, partner.company_id)
        req = TravelRequest(
            protocol=f"VX-TEST-{int(datetime.now(timezone.utc).timestamp())}",
            company_id=company.id,
            base_id=base.id,
            requested_by_user_id=partner.id,
            request_type="extra",
            requested_datetime=datetime.now(timezone.utc),
            origin="A",
            destination="B",
            quantity=1,
            vehicle_type_requested="sedan",
            cost_center="CC",
            reason="teste",
            status=RequestStatus.CONFIRMED,
        )
        session.add(req)
        session.commit()
        session.refresh(req)
        req_id = req.id

    monkeypatch.setattr("app.routes.web.request_otp", lambda *_args, **_kwargs: (_ for _ in ()).throw(DomainError("Nao foi possivel enviar o OTP agora.")))

    resp = client.post(f"/partner/requests/{req_id}/otp", follow_redirects=False)
    assert resp.status_code == 303
    assert "/partner?message=" in resp.headers["location"]


def test_status_labels_by_profile_and_partner_cancel_action(client: TestClient):
    with Session(engine) as session:
        partner = session.exec(select(User).where(User.email == "parceiro@logtudo.local")).first()
        sup = session.exec(select(User).where(User.email == "supervisor@logtudo.local")).first()
        base = session.exec(select(Base)).first()
        company = session.get(Company, partner.company_id)

        req = TravelRequest(
            protocol=f"VX-LABEL-{int(datetime.now(timezone.utc).timestamp())}",
            company_id=company.id,
            base_id=base.id,
            requested_by_user_id=partner.id,
            request_type="extra",
            requested_datetime=datetime(2030, 1, 3, 10, 0, tzinfo=timezone.utc),
            origin="A",
            destination="B",
            quantity=1,
            vehicle_type_requested="sedan",
            cost_center="CC",
            reason="teste",
            status=RequestStatus.CONFIRMED,
        )
        session.add(req)
        session.flush()
        session.add(
            OperationalConfirmation(
                request_id=req.id,
                supervisor_user_id=sup.id,
                decision_type=DecisionType.CONFIRM,
                approved_quantity=1,
                confirmed_datetime=datetime(2030, 1, 3, 10, 0, tzinfo=timezone.utc),
                confirmed_vehicle_type="sedan",
                tariff_value=100.0,
            )
        )
        session.commit()

    login(client, "parceiro@logtudo.local", "parceiro123")
    partner_page = client.get("/partner")
    assert "Aprovação pendente de aceite" in partner_page.text
    assert "Cancelar pedido" in partner_page.text

    login(client, "supervisor@logtudo.local", "supervisor123")
    supervisor_page = client.get("/empresa/operacoes")
    assert "Confirmado" in supervisor_page.text


def test_manager_register_partner_with_new_company_and_multiple_bases(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")
    with Session(engine) as session:
        base1 = session.exec(select(Base)).first()
        base2 = Base(name="Base Feira", location="Feira de Santana", sla_minutes=30, min_advance_minutes=60)
        session.add(base2)
        session.commit()
        session.refresh(base2)
        base_ids = [base1.id, base2.id]

    resp = client.post(
        "/empresa/gerencial/partners/new",
        data={
            "full_name": "Parceiro Multi",
            "email": f"multi-{int(datetime.now(timezone.utc).timestamp())}@test.local",
            "password": "senha1234",
            "company_name": "Empresa Nova Multi",
            "phone": "71999887766",
            "base_ids": base_ids,
            "sla_minutes": 45,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/empresa/gerencial?message=" in resp.headers["location"]

    with Session(engine) as session:
        company = session.exec(select(Company).where(Company.name == "Empresa Nova Multi")).first()
        assert company is not None
        partner = session.exec(select(User).where(User.email.like("multi-%@test.local")).order_by(User.id.desc())).first()
        assert partner is not None
        assert partner.company_id == company.id
        links = session.exec(select(UserBaseLink).where(UserBaseLink.user_id == partner.id)).all()
        assert len(links) == 2
        cbs = session.exec(select(CompanyBase).where(CompanyBase.company_id == company.id)).all()
        assert len(cbs) >= 2


def test_manager_register_partner_reuses_existing_company(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")
    with Session(engine) as session:
        company = session.exec(select(Company).where(Company.name == "Parceiro Exemplo")).first()
        count_before = len(session.exec(select(Company).where(Company.name == "Parceiro Exemplo")).all())
        base = session.exec(select(Base)).first()

    resp = client.post(
        "/empresa/gerencial/partners/new",
        data={
            "full_name": "Parceiro Reuso",
            "email": f"reuso-{int(datetime.now(timezone.utc).timestamp())}@test.local",
            "password": "senha1234",
            "company_name": "Parceiro Exemplo",
            "phone": "71988776655",
            "base_ids": [base.id],
            "sla_minutes": 30,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    with Session(engine) as session:
        count_after = len(session.exec(select(Company).where(Company.name == "Parceiro Exemplo")).all())
        assert count_after == count_before
        partner = session.exec(select(User).where(User.email.like("reuso-%@test.local")).order_by(User.id.desc())).first()
        assert partner is not None
        assert partner.company_id == company.id


def test_supervisor_listing_shows_derived_companies(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")
    with Session(engine) as session:
        base1 = session.exec(select(Base)).first()
        base2 = Base(name="Base Camaçari", location="Camaçari", sla_minutes=30, min_advance_minutes=60)
        company2 = Company(name="Empresa Coberta 2", cnpj="22.222.222/0001-22")
        session.add(base2)
        session.add(company2)
        session.commit()
        session.refresh(base2)
        session.refresh(company2)
        session.add(CompanyBase(company_id=company2.id, base_id=base2.id, contract_sla_minutes=30))
        session.commit()
        base1_id = base1.id
        base2_id = base2.id

    email = f"supmulti-{int(datetime.now(timezone.utc).timestamp())}@test.local"
    resp = client.post(
        "/empresa/gerencial/supervisors/new",
        data={
            "full_name": "Supervisor Multi",
            "email": email,
            "password": "senha1234",
            "base_ids": [base1_id, base2_id],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    page = client.get("/empresa/gerencial")
    assert "Empresa Coberta 2" in page.text
