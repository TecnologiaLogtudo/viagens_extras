from fastapi.testclient import TestClient
import pytest
from datetime import datetime, timezone
from sqlmodel import Session, select

from app.db import engine
from app.main import app
from app.models import Base, Company, CompanyBase, DecisionType, Driver, EventLog, OperationalConfirmation, RequestStatus, UserBaseLink, UserCompanyBaseLink, TravelRequest, User, Vehicle
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

        # Link supervisor to request's base in the test
        cb = session.exec(select(CompanyBase).where(CompanyBase.base_id == base.id)).first()
        if cb:
            # Ensure the link doesn't already exist to prevent integrity errors
            exists = session.exec(
                select(UserCompanyBaseLink).where(
                    UserCompanyBaseLink.user_id == sup.id,
                    UserCompanyBaseLink.company_base_id == cb.id,
                )
            ).first()
            if not exists:
                session.add(UserCompanyBaseLink(user_id=sup.id, company_base_id=cb.id))
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
        data={"driver_ids": [str(driver_one_id), str(driver_two_id)]},
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


def test_manager_can_toggle_driver_active(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")

    with Session(engine) as session:
        base = session.exec(select(Base).order_by(Base.id)).first()
        assert base is not None
        base_id = base.id

    stamp = int(datetime.now(timezone.utc).timestamp())
    driver_name = f"Driver Active Toggle {stamp}"

    # Create driver
    client.post(
        "/empresa/motoristas/save",
        data={"name": driver_name, "phone": "71999887766", "base_id": base_id, "active": "1"},
        follow_redirects=False,
    )

    with Session(engine) as session:
        driver = session.exec(select(Driver).where(Driver.name == driver_name)).first()
        assert driver is not None
        assert driver.active is True
        driver_id = driver.id

    # Toggle to False
    resp1 = client.post(f"/empresa/motoristas/{driver_id}/toggle-active", follow_redirects=False)
    assert resp1.status_code == 303

    with Session(engine) as session:
        driver = session.get(Driver, driver_id)
        assert driver.active is False

    # Toggle back to True
    resp2 = client.post(f"/empresa/motoristas/{driver_id}/toggle-active", follow_redirects=False)
    assert resp2.status_code == 303

    with Session(engine) as session:
        driver = session.get(Driver, driver_id)
        assert driver.active is True


def test_manager_can_delete_single_driver(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")

    with Session(engine) as session:
        base = session.exec(select(Base).order_by(Base.id)).first()
        assert base is not None
        base_id = base.id

    stamp = int(datetime.now(timezone.utc).timestamp())
    driver_name = f"Driver Single Delete {stamp}"

    # Create driver
    client.post(
        "/empresa/motoristas/save",
        data={"name": driver_name, "phone": "71999887766", "base_id": base_id, "active": "1"},
        follow_redirects=False,
    )

    with Session(engine) as session:
        driver = session.exec(select(Driver).where(Driver.name == driver_name)).first()
        assert driver is not None
        driver_id = driver.id

    # Delete driver
    resp = client.post(f"/empresa/motoristas/{driver_id}/delete", follow_redirects=False)
    assert resp.status_code == 303

    with Session(engine) as session:
        assert session.get(Driver, driver_id) is None


def test_manager_can_create_driver_with_vehicle(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")

    with Session(engine) as session:
        base = session.exec(select(Base).order_by(Base.id)).first()
        assert base is not None
        base_id = base.id

    stamp = int(datetime.now(timezone.utc).timestamp())
    driver_name = f"Driver Vehicle {stamp}"
    plate = f"PLK{stamp % 10000:04d}"

    # Create driver with vehicle
    create_resp = client.post(
        "/empresa/motoristas/save",
        data={
            "name": driver_name,
            "phone": "71999887766",
            "base_id": base_id,
            "vehicle_type": "sedan",
            "plate": plate,
            "active": "1",
        },
        follow_redirects=False,
    )
    assert create_resp.status_code == 303

    with Session(engine) as session:
        driver = session.exec(select(Driver).where(Driver.name == driver_name)).first()
        assert driver is not None
        assert driver.vehicle_id is not None
        vehicle = session.get(Vehicle, driver.vehicle_id)
        assert vehicle is not None
        assert vehicle.plate == plate
        assert vehicle.vehicle_type == "SEDAN"
        driver_id = driver.id

    # Edit and update vehicle type
    edit_resp = client.post(
        "/empresa/motoristas/save",
        data={
            "driver_id": driver_id,
            "name": driver_name,
            "phone": "71999887766",
            "base_id": base_id,
            "vehicle_type": "van",
            "plate": plate,
            "active": "1",
        },
        follow_redirects=False,
    )
    assert edit_resp.status_code == 303

    with Session(engine) as session:
        driver = session.get(Driver, driver_id)
        vehicle = session.get(Vehicle, driver.vehicle_id)
        assert vehicle.vehicle_type == "VAN"


def test_manager_can_filter_drivers_by_base_and_vehicle_type(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")

    with Session(engine) as session:
        base = session.exec(select(Base).order_by(Base.id)).first()
        assert base is not None
        base_id = base.id

    stamp = int(datetime.now(timezone.utc).timestamp())
    driver_name = f"FilterDriver {stamp}"
    plate = f"FLT{stamp % 10000:04d}"

    # Create a specific driver with a unique category KOMBI
    client.post(
        "/empresa/motoristas/save",
        data={
            "name": driver_name,
            "phone": "71999887766",
            "base_id": base_id,
            "vehicle_type": "KOMBI",
            "plate": plate,
            "active": "1",
        },
        follow_redirects=False,
    )

    # 1. Query the fragment filter by base and vehicle type KOMBI
    resp_frag = client.get(
        f"/empresa/motoristas/fragments/list?filter_base_id={base_id}&filter_vehicle_type=KOMBI"
    )
    assert resp_frag.status_code == 200
    assert driver_name in resp_frag.text

    # 2. Query the fragment with a different filter (e.g. vehicle type MOTO)
    resp_frag_empty = client.get(
        f"/empresa/motoristas/fragments/list?filter_base_id={base_id}&filter_vehicle_type=MOTO"
    )
    assert resp_frag_empty.status_code == 200
    assert driver_name not in resp_frag_empty.text

    # 3. Query the main page with KOMBI filter
    resp_page = client.get(
        f"/empresa/motoristas?filter_base_id={base_id}&filter_vehicle_type=KOMBI"
    )
    assert resp_page.status_code == 200
    assert driver_name in resp_page.text


def test_manager_drivers_table_pagination(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")

    with Session(engine) as session:
        base = session.exec(select(Base).order_by(Base.id)).first()
        assert base is not None
        base_id = base.id
        
        # Create 15 drivers to test pagination (needs at least 13 to have 2 pages)
        for i in range(15):
            driver_name = f"PaginatedDriver {i}"
            driver = Driver(name=driver_name, phone="", base_id=base_id, active=True)
            session.add(driver)
        session.commit()

    # Query page 1
    resp_page_1 = client.get("/empresa/motoristas?page=1")
    assert resp_page_1.status_code == 200
    assert "1 / 2" in resp_page_1.text  # pagination indicator: page 1 of 2
    assert "Próxima" in resp_page_1.text

    # Query page 2
    resp_page_2 = client.get("/empresa/motoristas?page=2")
    assert resp_page_2.status_code == 200
    assert "2 / 2" in resp_page_2.text
    assert "Anterior" in resp_page_2.text


def test_download_comprovante_route(client: TestClient):
    import os
    from pathlib import Path
    from app.models import UserRole
    from app.auth import hash_password
    from app.models import Notification, NotificationType

    # 1. Setup a completed travel request
    with Session(engine) as session:
        partner = session.exec(select(User).where(User.email == "parceiro@logtudo.local")).first()
        base = session.exec(select(Base)).first()
        company = session.get(Company, partner.company_id)
        
        req = TravelRequest(
            protocol=f"VX-COMP-{int(datetime.now(timezone.utc).timestamp())}",
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
            reason="teste comprovante",
            status=RequestStatus.COMPLETED,
        )
        session.add(req)
        session.flush()

        notification = Notification(
            request_id=req.id,
            type=NotificationType.OTP_SENT,
            recipient=partner.email,
            subject="OTP",
            body="Seu codigo OTP e: 633081. Valido por 10 minutos."
        )
        session.add(notification)
        session.commit()
        session.refresh(req)
        req_id = req.id
        protocol = req.protocol
        base_id = base.id

    # File path where PDF is generated
    base_dir = Path(__file__).resolve().parent.parent
    pdf_path = base_dir / "app" / "data" / "documents" / f"{protocol}.pdf"

    try:
        # 2. Try accessing while not logged in
        resp = client.get(f"/requests/{req_id}/comprovante", follow_redirects=False)
        assert resp.status_code == 303 or resp.status_code == 401
        
        # 3. Log in as partner and download (success)
        login(client, "parceiro@logtudo.local", "parceiro123")
        resp = client.get(f"/requests/{req_id}/comprovante")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert f'attachment; filename="comprovante_{protocol}.pdf"' in resp.headers["content-disposition"]
        assert pdf_path.exists()
        
        # 4. Try as partner of another company
        with Session(engine) as session:
            other_company = Company(name="Outra Cia", cnpj="99.999.999/0001-99")
            session.add(other_company)
            session.flush()
            other_partner = User(
                full_name="Outro Parceiro",
                email="outro_parceiro@test.local",
                role=UserRole.PARTNER_REQUESTER,
                company_id=other_company.id,
                company_name=other_company.name,
                password_hash=hash_password("senha123"),
                is_active=True,
            )
            session.add(other_partner)
            session.commit()
            
        login(client, "outro_parceiro@test.local", "senha123")
        resp = client.get(f"/requests/{req_id}/comprovante")
        assert resp.status_code == 403

        # 5. Log in as supervisor and download (success, if supervisor has base permission)
        with Session(engine) as session:
            sup = session.exec(select(User).where(User.email == "supervisor@logtudo.local")).first()
            # Link supervisor to request's base if not linked
            cb = session.exec(select(CompanyBase).where(CompanyBase.base_id == base_id)).first()
            if cb:
                exists = session.exec(
                    select(UserCompanyBaseLink).where(
                        UserCompanyBaseLink.user_id == sup.id,
                        UserCompanyBaseLink.company_base_id == cb.id,
                    )
                ).first()
                if not exists:
                    session.add(UserCompanyBaseLink(user_id=sup.id, company_base_id=cb.id))
            session.commit()
            
        login(client, "supervisor@logtudo.local", "supervisor123")
        resp = client.get(f"/requests/{req_id}/comprovante")
        assert resp.status_code == 200

        # 6. Try as supervisor without base permission
        with Session(engine) as session:
            other_base = Base(name="PE_TEST", location="Recife Test", sla_minutes=30, min_advance_minutes=60)
            session.add(other_base)
            session.flush()
            other_sup = User(
                full_name="Outro Sup",
                email="outro_sup@test.local",
                role=UserRole.BASE_SUPERVISOR,
                base_id=other_base.id,
                company_name="Logtudo",
                password_hash=hash_password("senha123"),
                is_active=True,
            )
            session.add(other_sup)
            session.commit()
            
        login(client, "outro_sup@test.local", "senha123")
        resp = client.get(f"/requests/{req_id}/comprovante")
        assert resp.status_code == 403

        # 7. Log in as manager and download (success)
        login(client, "gerente@logtudo.local", "gerente123")
        resp = client.get(f"/requests/{req_id}/comprovante")
        assert resp.status_code == 200

        # 8. Test on-the-fly regeneration when physical file is deleted
        if pdf_path.exists():
            os.remove(pdf_path)
        assert not pdf_path.exists()
        
        # Request again (will trigger regeneration)
        resp = client.get(f"/requests/{req_id}/comprovante")
        assert resp.status_code == 200
        assert pdf_path.exists()
        
    finally:
        # Cleanup physical file
        if pdf_path.exists():
            try:
                os.remove(pdf_path)
            except Exception:
                pass


def test_supervisor_panel_driver_filtering(client: TestClient):
    login(client, "supervisor@logtudo.local", "supervisor123")

    with Session(engine) as session:
        partner = session.exec(select(User).where(User.email == "parceiro@logtudo.local")).first()
        sup = session.exec(select(User).where(User.email == "supervisor@logtudo.local")).first()
        sup_id = sup.id
        base = session.exec(select(Base)).first()
        company = session.get(Company, partner.company_id)

        # Create request for SEDAN
        req = TravelRequest(
            protocol=f"VX-FILT-{int(datetime.now(timezone.utc).timestamp())}",
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
            status=RequestStatus.TRIAGE,
        )
        session.add(req)
        session.flush()

        # Link supervisor to request's base in the test
        cb = session.exec(select(CompanyBase).where(CompanyBase.base_id == base.id)).first()
        if cb:
            exists = session.exec(
                select(UserCompanyBaseLink).where(
                    UserCompanyBaseLink.user_id == sup_id,
                    UserCompanyBaseLink.company_base_id == cb.id,
                )
            ).first()
            if not exists:
                session.add(UserCompanyBaseLink(user_id=sup_id, company_base_id=cb.id))

        # Create three drivers with unique plates to avoid conflicts
        stamp = int(datetime.now(timezone.utc).timestamp())
        v_sedan = Vehicle(plate=f"SED{stamp%10000:04d}", vehicle_type="SEDAN", base_id=base.id, active=True)
        session.add(v_sedan)
        session.flush()
        d_sedan = Driver(name="Driver Sedan Test", phone="11911111111", base_id=base.id, vehicle_id=v_sedan.id, active=True)
        session.add(d_sedan)

        v_van = Vehicle(plate=f"VAN{stamp%10000:04d}", vehicle_type="VAN", base_id=base.id, active=True)
        session.add(v_van)
        session.flush()
        d_van = Driver(name="Driver Van Test", phone="11922222222", base_id=base.id, vehicle_id=v_van.id, active=True)
        session.add(d_van)

        d_none = Driver(name="Driver No Vehicle Test", phone="11933333333", base_id=base.id, vehicle_id=None, active=True)
        session.add(d_none)

        session.commit()
        session.refresh(req)
        req_id = req.id

    # 1. Access supervisor panel with requested vehicle_type = sedan (no confirmation yet)
    resp = client.get(f"/empresa/requests/{req_id}/supervisor-panel")
    assert resp.status_code == 200
    assert "Driver Sedan Test" in resp.text
    assert "Driver No Vehicle Test" in resp.text
    assert "Driver Van Test" not in resp.text

    # 2. Now add confirmation choosing VAN
    with Session(engine) as session:
        session.add(
            OperationalConfirmation(
                request_id=req_id,
                supervisor_user_id=sup_id,
                decision_type=DecisionType.CONFIRM,
                approved_quantity=1,
                confirmed_datetime=datetime(2030, 1, 3, 10, 0, tzinfo=timezone.utc),
                confirmed_vehicle_type="van",
                tariff_value=120.0,
            )
        )
        session.commit()

    # 3. Access supervisor panel again (should filter by confirmed vehicle_type = van)
    resp = client.get(f"/empresa/requests/{req_id}/supervisor-panel")
    assert resp.status_code == 200
    assert "Driver Van Test" in resp.text
    assert "Driver No Vehicle Test" in resp.text
    assert "Driver Sedan Test" not in resp.text
