from fastapi.testclient import TestClient
import pytest
from datetime import datetime, timezone
from sqlmodel import Session, select

from app.db import engine
from app.main import app
from app.models import Base, Company, CompanyBase, DecisionType, Driver, EventLog, OperationalConfirmation, RequestStatus, UserBaseLink, TravelRequest, User
from app.routes.web import _event_matches_user, _resolve_company_from_base_ids
from app.services.workflow import DomainError


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
        company = Company(name="Empresa Nova Multi", cnpj=f"00.000.000/0001-{int(datetime.now(timezone.utc).timestamp()) % 99:02d}")
        base1 = Base(name="Base Feira", location="Feira de Santana", sla_minutes=30, min_advance_minutes=60)
        base2 = Base(name="Base Itabuna", location="Itabuna", sla_minutes=45, min_advance_minutes=90)
        session.add(company)
        session.add(base1)
        session.add(base2)
        session.flush()
        link1 = CompanyBase(company_id=company.id, base_id=base1.id, contract_sla_minutes=30)
        link2 = CompanyBase(company_id=company.id, base_id=base2.id, contract_sla_minutes=45)
        session.add(link1)
        session.add(link2)
        session.commit()
        company_id = company.id
        link_ids = [link1.id, link2.id]

    resp = client.post(
        "/empresa/gerencial/partners/new",
        data={
            "full_name": "Parceiro Multi",
            "email": f"multi-{int(datetime.now(timezone.utc).timestamp())}@test.local",
            "password": "senha1234",
            "phone": "71999887766",
            "company_base_link_ids": link_ids,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/empresa/gerencial?message=" in resp.headers["location"]

    with Session(engine) as session:
        company = session.get(Company, company_id)
        assert company is not None
        partner = session.exec(select(User).where(User.email.like("multi-%@test.local")).order_by(User.id.desc())).first()
        assert partner is not None
        assert partner.company_id == company_id
        links = session.exec(select(UserBaseLink).where(UserBaseLink.user_id == partner.id)).all()
        assert len(links) == 2
        cbs = session.exec(select(CompanyBase).where(CompanyBase.company_id == company_id)).all()
        assert len(cbs) >= 2


def test_manager_register_partner_reuses_existing_company(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")
    with Session(engine) as session:
        company_base = None
        for candidate in session.exec(select(CompanyBase).order_by(CompanyBase.company_id, CompanyBase.base_id)).all():
            if len(session.exec(select(CompanyBase).where(CompanyBase.base_id == candidate.base_id)).all()) == 1:
                company_base = candidate
                break
        assert company_base is not None
        company = session.get(Company, company_base.company_id)
        assert company is not None
        count_before = len(session.exec(select(Company).where(Company.name == company.name)).all())

    resp = client.post(
        "/empresa/gerencial/partners/new",
        data={
            "full_name": "Parceiro Reuso",
            "email": f"reuso-{int(datetime.now(timezone.utc).timestamp())}@test.local",
            "password": "senha1234",
            "phone": "71988776655",
            "base_ids": [company_base.base_id],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/empresa/gerencial?message=" in resp.headers["location"]

    with Session(engine) as session:
        count_after = len(session.exec(select(Company).where(Company.name == company.name)).all())
        assert count_after == count_before
        partner = session.exec(select(User).where(User.email.like("reuso-%@test.local")).order_by(User.id.desc())).first()
        assert partner is not None
        assert partner.company_id == company.id


def test_manager_register_partner_uses_company_from_selected_company_base_links(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")
    with Session(engine) as session:
        company_a = Company(name="Empresa A Compartilhada", cnpj=f"00.000.000/0001-{int(datetime.now(timezone.utc).timestamp()) % 98 + 1:02d}")
        company_b = Company(name="Empresa B Compartilhada", cnpj=f"00.000.000/0001-{int(datetime.now(timezone.utc).timestamp()) % 97 + 2:02d}")
        shared_base = Base(name="Base Compartilhada", location="Salvador", sla_minutes=30, min_advance_minutes=60)
        unique_base = Base(name="Base Exclusiva", location="Camaçari", sla_minutes=35, min_advance_minutes=80)
        session.add(company_a)
        session.add(company_b)
        session.add(shared_base)
        session.add(unique_base)
        session.flush()
        shared_link_a = CompanyBase(company_id=company_a.id, base_id=shared_base.id, contract_sla_minutes=30)
        shared_link_b = CompanyBase(company_id=company_b.id, base_id=shared_base.id, contract_sla_minutes=30)
        unique_link_a = CompanyBase(company_id=company_a.id, base_id=unique_base.id, contract_sla_minutes=35)
        session.add(shared_link_a)
        session.add(shared_link_b)
        session.add(unique_link_a)
        session.commit()

        company_a_id = company_a.id
        selected_link_ids = [shared_link_a.id, unique_link_a.id]

    resp = client.post(
        "/empresa/gerencial/partners/new",
        data={
            "full_name": "Parceiro Base Existente",
            "email": f"base-existente-{int(datetime.now(timezone.utc).timestamp())}@test.local",
            "password": "senha1234",
            "phone": "71988776655",
            "company_base_link_ids": selected_link_ids,
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "/empresa/gerencial?message=" in resp.headers["location"]

    with Session(engine) as session:
        company = session.get(Company, company_a_id)
        assert company is not None
        partner = session.exec(
            select(User)
            .where(User.email.like("base-existente-%@test.local"))
            .order_by(User.id.desc())
        ).first()
        assert partner is not None
        assert partner.company_id == company_a_id


def test_supervisor_listing_shows_derived_companies(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")
    with Session(engine) as session:
        base1 = session.exec(select(Base).where(Base.name == "BA")).first()
        base2 = session.exec(select(Base).where(Base.name == "PE")).first()
        assert base1 is not None
        assert base2 is not None
        base1_id = base1.id
        base2_id = base2.id

    email = f"supmulti-{int(datetime.now(timezone.utc).timestamp())}@test.local"
    resp = client.post(
        "/empresa/gerencial/supervisors/new",
        data={
            "full_name": "Supervisor Multi",
            "email": email,
            "phone": "71998765213",
            "password": "senha1234",
            "base_ids": [base1_id, base2_id],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303

    page = client.get("/empresa/gerencial")
    assert "Danone" in page.text
    assert "Lactalis" in page.text
    assert base1.name in page.text
    assert 'class="company-group"' in page.text
    assert 'name="base_ids"' in page.text

    with Session(engine) as session:
        created = session.exec(select(User).where(User.email == email)).first()
        assert created is not None
        assert created.phone == "71 99876-5213"

    edit_page = client.get(f"/empresa/gerencial?edit_supervisor_id={created.id}")
    page_text = edit_page.text
    for base_id in (base1_id, base2_id):
        marker = f'value="{base_id}"'
        marker_index = page_text.find(marker)
        assert marker_index != -1
        assert "checked" in page_text[marker_index:marker_index + 180]


def test_fragment_routes_require_and_render_expected_blocks(client: TestClient):
    login(client, "parceiro@logtudo.local", "parceiro123")
    partner_metrics = client.get("/partner/fragments/metrics")
    partner_requests = client.get("/partner/fragments/requests")
    assert partner_metrics.status_code == 200
    assert "partner-metrics" in partner_metrics.text
    assert partner_requests.status_code == 200
    assert "partner-requests" in partner_requests.text

    login(client, "gerente@logtudo.local", "gerente123")
    op_metrics = client.get("/empresa/operacoes/fragments/metrics")
    op_requests = client.get("/empresa/operacoes/fragments/requests")
    assert op_metrics.status_code == 200
    assert "operations-metrics" in op_metrics.text
    assert op_requests.status_code == 200
    assert "operations-requests" in op_requests.text

    login(client, "gerente@logtudo.local", "gerente123")
    mgr_sup = client.get("/empresa/gerencial/fragments/supervisors")
    mgr_partners = client.get("/empresa/gerencial/fragments/partners")
    mgr_contracts = client.get("/empresa/gerencial/fragments/contracts")
    assert mgr_sup.status_code == 200
    assert "manager-supervisors" in mgr_sup.text
    assert mgr_partners.status_code == 200
    assert "manager-partners" in mgr_partners.text
    assert mgr_contracts.status_code == 200
    assert "manager-contracts" in mgr_contracts.text


def test_sse_requires_auth(client: TestClient):
    client.post("/logout")
    resp = client.get("/events/stream")
    assert resp.status_code == 401


def test_sse_event_filtering_logic_by_role():
    with Session(engine) as session:
        partner = session.exec(select(User).where(User.email == "parceiro@logtudo.local")).first()
        manager = session.exec(select(User).where(User.email == "gerente@logtudo.local")).first()
        base = session.exec(select(Base)).first()
        company = session.get(Company, partner.company_id)
        req = TravelRequest(
            protocol=f"VX-SSE-{int(datetime.now(timezone.utc).timestamp())}",
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
            status=RequestStatus.SUBMITTED,
        )
        session.add(req)
        session.flush()
        manager_event = EventLog(event_type="manager_data_changed", payload="test_mgr")
        request_event = EventLog(request_id=req.id, event_type="request_changed", payload="test_request")
        session.add(manager_event)
        session.add(request_event)
        session.commit()
        session.refresh(manager_event)
        session.refresh(request_event)
        assert _event_matches_user(session, manager_event, manager) is True
        assert _event_matches_user(session, manager_event, partner) is False
        assert _event_matches_user(session, request_event, manager) is True


def test_manager_can_add_edit_and_bulk_delete_drivers(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")

    with Session(engine) as session:
        base = session.exec(select(Base).order_by(Base.id)).first()
        assert base is not None
        base_id = base.id

    page = client.get("/empresa/motoristas")
    assert page.status_code == 200
    assert "Telefone" in page.text
    assert "Cadastrar Motorista" in page.text

    stamp = int(datetime.now(timezone.utc).timestamp())
    driver_one_name = f"Motorista Teste {stamp}"
    driver_two_name = f"Motorista Teste B {stamp}"

    create_one = client.post(
        "/empresa/motoristas/save",
        data={"name": driver_one_name, "phone": "71999887766", "base_id": base_id, "active": "1"},
        follow_redirects=False,
    )
    assert create_one.status_code == 303

    create_two = client.post(
        "/empresa/motoristas/save",
        data={"name": driver_two_name, "phone": "", "base_id": base_id, "active": "1"},
        follow_redirects=False,
    )
    assert create_two.status_code == 303

    with Session(engine) as session:
        driver_one = session.exec(select(Driver).where(Driver.name == driver_one_name)).first()
        driver_two = session.exec(select(Driver).where(Driver.name == driver_two_name)).first()
        assert driver_one is not None
        assert driver_two is not None
        assert driver_one.phone == "71 99988-7766"
        driver_one_id = driver_one.id
        driver_two_id = driver_two.id

    edit = client.post(
        "/empresa/motoristas/save",
        data={
            "driver_id": driver_one_id,
            "name": driver_one_name,
            "phone": "71900001111",
            "base_id": base_id,
            "active": "1",
        },
        follow_redirects=False,
    )
    assert edit.status_code == 303

    with Session(engine) as session:
        driver_one = session.get(Driver, driver_one_id)
        assert driver_one is not None
        assert driver_one.phone == "71 90000-1111"

    delete_bulk = client.post(
        "/empresa/motoristas/bulk-delete",
        data=[("driver_ids", driver_one_id), ("driver_ids", driver_two_id)],
        follow_redirects=False,
    )
    assert delete_bulk.status_code == 303

    with Session(engine) as session:
        assert session.get(Driver, driver_one_id) is None
        assert session.get(Driver, driver_two_id) is None


def test_resolve_company_from_selected_bases_prefers_existing_company():
    from sqlmodel import SQLModel, create_engine

    test_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(test_engine)

    with Session(test_engine) as session:
        company_a = Company(name="Empresa A", cnpj="00.000.000/0001-01")
        company_b = Company(name="Empresa B", cnpj="00.000.000/0001-02")
        base_a = Base(name="BA", location="Salvador")
        base_b = Base(name="PE", location="Recife")
        session.add(company_a)
        session.add(company_b)
        session.add(base_a)
        session.add(base_b)
        session.flush()
        session.add(CompanyBase(company_id=company_a.id, base_id=base_a.id))
        session.add(CompanyBase(company_id=company_b.id, base_id=base_b.id))
        session.commit()

        resolved = _resolve_company_from_base_ids(session, [base_a.id])
        assert resolved is not None
        assert resolved.id == company_a.id
