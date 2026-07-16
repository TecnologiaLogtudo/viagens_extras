import pytest
from datetime import datetime, timezone
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import (
    Base,
    Company,
    CompanyBase,
    DecisionType,
    Driver,
    DriverActivityStatus,
    RequestStatus,
    TravelRequestCreate,
    User,
    UserRole,
    Vehicle,
)
from app.services.workflow import (
    can_partner_cancel_request,
    cancel_request,
    DomainError,
    complete_trip,
    create_request,
    dispatch_trip,
    list_billable_requests,
    request_otp,
    sign_acceptance,
    triage_request,
)


@pytest.fixture()
def session():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        company = Company(name="Comp", cnpj="00")
        base = Base(name="Base", location="SSA", sla_minutes=30, min_advance_minutes=60)
        session.add(company)
        session.add(base)
        session.flush()

        session.add(CompanyBase(company_id=company.id, base_id=base.id))
        partner = User(full_name="P", email="p@test", role=UserRole.PARTNER_REQUESTER, company_id=company.id)
        sup = User(full_name="S", email="s@test", role=UserRole.BASE_SUPERVISOR, base_id=base.id)
        fin = User(full_name="F", email="f@test", role=UserRole.FINANCE_READONLY)
        session.add(partner)
        session.add(sup)
        session.add(fin)
        session.flush()

        session.add(Driver(name="D1", phone="1", base_id=base.id))
        session.add(Vehicle(plate="AAA1A11", vehicle_type="sedan", base_id=base.id))
        session.commit()
        yield session


def _mk_request(session):
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    base = session.exec(select(Base)).first()
    payload = TravelRequestCreate(
        base_id=base.id,
        request_type="Viagem extra NILO",
        requested_datetime=datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=1,
        vehicle_type_requested="sedan",
        cost_center="CC",
        reason="motivo",
    )
    return create_request(session, partner, payload)


def _mock_email_delivery(monkeypatch):
    monkeypatch.setattr("app.services.workflow.send_email", lambda *_args, **_kwargs: None)


def test_min_advance_validation(session):
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    base = session.exec(select(Base)).first()
    
    # 1. Past date validation
    payload_past = TravelRequestCreate(
        base_id=base.id,
        request_type="Viagem extra NILO",
        requested_datetime=datetime(2020, 1, 2, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=1,
        vehicle_type_requested="sedan",
        cost_center="CC",
        reason="motivo",
    )
    with pytest.raises(DomainError) as exc_info:
        create_request(session, partner, payload_past)
    assert "no futuro" in str(exc_info.value)

    # 2. Partner min_advance_minutes validation
    from datetime import timedelta
    partner.min_advance_minutes = 60
    session.add(partner)
    session.commit()
    
    payload_insufficient = TravelRequestCreate(
        base_id=base.id,
        request_type="Viagem extra NILO",
        requested_datetime=datetime.now(timezone.utc) + timedelta(minutes=30),
        origin="A",
        destination="B",
        quantity=1,
        vehicle_type_requested="sedan",
        cost_center="CC",
        reason="motivo",
    )
    with pytest.raises(DomainError) as exc_info:
        create_request(session, partner, payload_insufficient)
    assert "Antecedência mínima" in str(exc_info.value)


def test_full_happy_path_billable(session, monkeypatch):
    _mock_email_delivery(monkeypatch)
    req = _mk_request(session)
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    triage_request(
        session,
        sup,
        req,
        __import__("app.models", fromlist=["TriageDecisionPayload"]).TriageDecisionPayload(
            decision_type=DecisionType.CONFIRM,
            approved_quantity=1,
            confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
            confirmed_vehicle_type="sedan",
            tariff_value=120.0,
        ),
    )

    req = session.get(type(req), req.id)
    assert req.status == RequestStatus.COMPLETED
    billable = list_billable_requests(session, None, None)
    assert len(billable) == 1



def test_cancel_allowed_for_submitted_and_triage(session):
    req = _mk_request(session)
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    canceled = cancel_request(session, sup, req, reason="mudanca")
    assert canceled.status == RequestStatus.CANCELED

    req2 = _mk_request(session)
    req2.status = RequestStatus.TRIAGE
    session.add(req2)
    session.commit()
    canceled2 = cancel_request(session, sup, req2, reason="mudanca")
    assert canceled2.status == RequestStatus.CANCELED


def test_cancel_allowed_for_confirmed_by_supervisor(session, monkeypatch):
    _mock_email_delivery(monkeypatch)
    req = _mk_request(session)
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    triage_request(
        session,
        sup,
        req,
        __import__("app.models", fromlist=["TriageDecisionPayload"]).TriageDecisionPayload(
            decision_type=DecisionType.CONFIRM,
            approved_quantity=1,
            confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
            confirmed_vehicle_type="sedan",
            tariff_value=120.0,
        ),
    )
    req = session.get(type(req), req.id)
    assert can_partner_cancel_request(session, req) is False
    with pytest.raises(DomainError, match="Não é possível cancelar uma viagem já concluída"):
        cancel_request(session, sup, req, reason="mudanca")


def test_partner_cancel_always_blocked(session):
    req = _mk_request(session)
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    assert can_partner_cancel_request(session, req) is False
    with pytest.raises(DomainError, match="Somente a equipe Logtudo pode cancelar solicitações"):
        cancel_request(session, partner, req, reason="mudanca")


def test_supervisor_cancel_allowed_for_active_statuses(session):
    req = _mk_request(session)
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    for status in (
        RequestStatus.ACCEPTED,
        RequestStatus.IN_EXECUTION,
    ):
        req.status = status
        session.add(req)
        session.commit()
        canceled = cancel_request(session, sup, req, reason="mudanca")
        assert canceled.status == RequestStatus.CANCELED


def test_create_request_multi_vehicle_success(session):
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    base = session.exec(select(Base)).first()
    
    payload = TravelRequestCreate(
        base_id=base.id,
        request_type="extra",
        requested_datetime=datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=2,
        vehicle_type_requested="truck, fiorino",
        cost_center="CC",
        reason="motivo",
    )
    req = create_request(session, partner, payload)
    assert req.quantity == 2
    assert req.vehicle_type_requested == "truck, fiorino"


def test_create_request_multi_vehicle_mismatch(session):
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    base = session.exec(select(Base)).first()
    
    payload = TravelRequestCreate(
        base_id=base.id,
        request_type="extra",
        requested_datetime=datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=2,
        vehicle_type_requested="truck",
        cost_center="CC",
        reason="motivo",
    )
    with pytest.raises(DomainError, match="A quantidade de tipos de veículos solicitados deve ser igual"):
        create_request(session, partner, payload)


def test_triage_multi_vehicle_and_drivers(session):
    base = session.exec(select(Base)).first()
    d2 = Driver(name="D2", phone="2", base_id=base.id)
    session.add(d2)
    session.commit()
    
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    payload = TravelRequestCreate(
        base_id=base.id,
        request_type="extra",
        requested_datetime=datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=2,
        vehicle_type_requested="truck, fiorino",
        cost_center="CC",
        reason="motivo",
    )
    req = create_request(session, partner, payload)
    
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    drivers = session.exec(select(Driver).order_by(Driver.id)).all()
    driver1_id = drivers[0].id
    driver2_id = drivers[1].id
    
    with pytest.raises(DomainError, match="A quantidade de tipos de veículos confirmados deve ser igual"):
        triage_request(
            session,
            sup,
            req,
            __import__("app.models", fromlist=["TriageDecisionPayload"]).TriageDecisionPayload(
                decision_type=DecisionType.CONFIRM,
                approved_quantity=2,
                confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
                confirmed_vehicle_type="truck",
                tariff_value=120.0,
                driver_ids=f"{driver1_id}, {driver2_id}",
            ),
        )

    with pytest.raises(DomainError, match="A quantidade de motoristas vinculados deve ser igual"):
        triage_request(
            session,
            sup,
            req,
            __import__("app.models", fromlist=["TriageDecisionPayload"]).TriageDecisionPayload(
                decision_type=DecisionType.CONFIRM,
                approved_quantity=2,
                confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
                confirmed_vehicle_type="truck, fiorino",
                tariff_value=120.0,
                driver_ids=f"{driver1_id}",
            ),
        )

    triage_request(
        session,
        sup,
        req,
        __import__("app.models", fromlist=["TriageDecisionPayload"]).TriageDecisionPayload(
            decision_type=DecisionType.CONFIRM,
            approved_quantity=2,
            confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
            confirmed_vehicle_type="truck, fiorino",
            tariff_value=120.0,
            driver_ids=f"{driver1_id}, {driver2_id}",
        ),
    )
    
    conf = session.exec(
        select(__import__("app.models", fromlist=["OperationalConfirmation"]).OperationalConfirmation)
        .where(
            __import__("app.models", fromlist=["OperationalConfirmation"]).OperationalConfirmation.request_id == req.id
        )
    ).first()
    assert conf is not None
    assert conf.approved_quantity == 2
    assert conf.confirmed_vehicle_type == "truck, fiorino"
    assert conf.driver_id == driver1_id
    assert conf.driver_ids == f"{driver1_id}, {driver2_id}"


def test_triage_multiple_drivers_completes(session, monkeypatch):
    _mock_email_delivery(monkeypatch)
    base = session.exec(select(Base)).first()
    d2 = Driver(name="D2", phone="2", base_id=base.id)
    session.add(d2)
    session.commit()
    
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    payload = TravelRequestCreate(
        base_id=base.id,
        request_type="extra",
        requested_datetime=datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=2,
        vehicle_type_requested="truck, fiorino",
        cost_center="CC",
        reason="motivo",
    )
    req = create_request(session, partner, payload)
    
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    drivers = session.exec(select(Driver).order_by(Driver.id)).all()
    driver1 = drivers[0]
    driver2 = drivers[1]
    
    triage_request(
        session,
        sup,
        req,
        __import__("app.models", fromlist=["TriageDecisionPayload"]).TriageDecisionPayload(
            decision_type=DecisionType.CONFIRM,
            approved_quantity=2,
            confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
            confirmed_vehicle_type="truck, fiorino",
            tariff_value=120.0,
            driver_ids=f"{driver1.id}, {driver2.id}",
        ),
    )
    
    req = session.get(type(req), req.id)
    assert req.status == RequestStatus.COMPLETED
    billable = list_billable_requests(session, None, None)
    assert len(billable) == 1


def test_quote_triage_flow(session, monkeypatch):
    _mock_email_delivery(monkeypatch)
    req = _mk_request(session)
    req.request_type = "Cotação de preço"
    session.add(req)
    session.commit()

    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    
    # Triage with 0 tariff must fail
    with pytest.raises(DomainError, match="O valor da tarifa é obrigatório para cotação de preço."):
        triage_request(
            session,
            sup,
            req,
            __import__("app.models", fromlist=["TriageDecisionPayload"]).TriageDecisionPayload(
                decision_type=DecisionType.CONFIRM,
                approved_quantity=1,
                confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
                confirmed_vehicle_type="sedan",
                tariff_value=0.0,
            ),
        )

    # Triage with correct tariff must succeed and transition to CONFIRMED without PDF
    triage_request(
        session,
        sup,
        req,
        __import__("app.models", fromlist=["TriageDecisionPayload"]).TriageDecisionPayload(
            decision_type=DecisionType.CONFIRM,
            approved_quantity=1,
            confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
            confirmed_vehicle_type="sedan",
            tariff_value=150.0,
        ),
    )
    req = session.get(type(req), req.id)
    assert req.status == RequestStatus.CONFIRMED
    # Verify no PDF document is generated yet
    from app.models import Document
    doc = session.exec(select(Document).where(Document.request_id == req.id)).first()
    assert doc is None


def test_cancel_request_sends_email(session, monkeypatch):
    req = _mk_request(session)
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()

    sent_emails = []
    def mock_send_email(to_email, subject, body, attachment_path=None):
        sent_emails.append({
            "to_email": to_email,
            "subject": subject,
            "body": body,
            "attachment_path": attachment_path
        })
    monkeypatch.setattr("app.services.workflow.send_email", mock_send_email)

    cancel_request(session, sup, req, reason="Falta de motorista disponível")

    assert len(sent_emails) == 2
    cancel_email_partner = sent_emails[0]
    assert "cancelada" in cancel_email_partner["subject"].lower()
    assert "Falta de motorista disponível" in cancel_email_partner["body"]
    assert cancel_email_partner["attachment_path"] is None


def test_complete_trip_sends_email_with_pdf(session, monkeypatch):
    req = _mk_request(session)
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    
    # Manually transition req status to IN_EXECUTION and create Dispatch row for testing complete_trip
    req.status = RequestStatus.IN_EXECUTION
    session.add(req)
    
    from app.models import Dispatch, Driver, Vehicle
    driver = session.exec(select(Driver)).first()
    vehicle = session.exec(select(Vehicle)).first()
    dispatch = Dispatch(
        request_id=req.id,
        driver_id=driver.id,
        vehicle_id=vehicle.id,
        planned_departure_at=datetime.now(timezone.utc),
    )
    session.add(dispatch)
    session.commit()

    sent_emails = []
    def mock_send_email(to_email, subject, body, attachment_path=None):
        sent_emails.append({
            "to_email": to_email,
            "subject": subject,
            "body": body,
            "attachment_path": attachment_path
        })
    monkeypatch.setattr("app.services.workflow.send_email", mock_send_email)

    complete_trip(
        session,
        sup,
        req,
        actual_departure_at=datetime.now(timezone.utc),
        actual_arrival_at=datetime.now(timezone.utc),
        occurrences="Sem intercorrências",
    )

    assert len(sent_emails) == 1
    comp_email = sent_emails[0]
    assert "Pedido" in comp_email["subject"]
    assert "concluído com sucesso" in comp_email["subject"]
    assert comp_email["attachment_path"] is not None
    assert comp_email["attachment_path"].endswith(f"{req.protocol}.pdf")


def test_create_request_notifications_for_all_profiles(session, monkeypatch):
    _mock_email_delivery(monkeypatch)
    from app.models import Notification, NotificationType
    
    # Clean existing notifications
    for notif in session.exec(select(Notification)).all():
        session.delete(notif)
    session.commit()
    
    base = session.exec(select(Base)).first()
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    
    # Ensure there is an active logistics manager
    mgr = User(full_name="M", email="m@test", role=UserRole.LOGISTICS_MANAGER, is_active=True)
    session.add(mgr)
    
    # Ensure supervisor is active and has access
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    sup.is_active = True
    session.add(sup)
    session.commit()
    
    # 1. Create a "Viagem extra" request
    payload_extra = TravelRequestCreate(
        base_id=base.id,
        request_type="Viagem extra",
        requested_datetime=datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=1,
        vehicle_type_requested="sedan",
        cost_center="CC",
        reason="motivo",
    )
    req_extra = create_request(session, partner, payload_extra)
    
    # Verify notifications for req_extra
    notifs_extra = session.exec(
        select(Notification)
        .where(Notification.request_id == req_extra.id)
    ).all()
    
    # Both supervisor (id=sup.id) and manager (id=mgr.id) should have notifications
    sup_notifs = [n for n in notifs_extra if n.user_id == sup.id]
    mgr_notifs = [n for n in notifs_extra if n.user_id == mgr.id]
    
    assert len(sup_notifs) > 0
    assert len(mgr_notifs) > 0
    # Check that in-app notification exists for both
    assert any(n.channel == "in_app" for n in sup_notifs)
    assert any(n.channel == "in_app" for n in mgr_notifs)
    
    # 2. Create a "Cotação de preço" request
    payload_quote = TravelRequestCreate(
        base_id=base.id,
        request_type="Cotação de preço",
        requested_datetime=datetime(2030, 1, 3, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=1,
        vehicle_type_requested="sedan",
        cost_center="CC",
        reason="motivo",
    )
    req_quote = create_request(session, partner, payload_quote)
    
    # Verify notifications for req_quote
    notifs_quote = session.exec(
        select(Notification)
        .where(Notification.request_id == req_quote.id)
    ).all()
    
    sup_quote_notifs = [n for n in notifs_quote if n.user_id == sup.id]
    mgr_quote_notifs = [n for n in notifs_quote if n.user_id == mgr.id]
    
    assert len(sup_quote_notifs) > 0
    assert len(mgr_quote_notifs) > 0
    assert any(n.channel == "in_app" for n in sup_quote_notifs)
    assert any(n.channel == "in_app" for n in mgr_quote_notifs)


def test_supervisor_strict_scoping(session):
    from app.services.workflow import supervisor_can_access_request
    from app.models import UserCompanyBaseLink, TravelRequest, TravelRequestCreate

    # 1. Setup two companies and one base
    company_a = session.exec(select(Company)).first()
    company_b = Company(name="Company B", cnpj="11")
    session.add(company_b)
    session.flush()

    base = session.exec(select(Base)).first()
    
    # Create CompanyBase link for Company B
    cb_b = CompanyBase(company_id=company_b.id, base_id=base.id)
    session.add(cb_b)
    session.flush()

    # 2. Setup a supervisor
    supervisor = User(
        full_name="Supervisor Strict",
        email="sup_strict@test.com",
        role=UserRole.BASE_SUPERVISOR,
        base_id=base.id
    )
    session.add(supervisor)
    session.flush()

    # Create travel request for Company A
    partner_a = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    payload_a = TravelRequestCreate(
        base_id=base.id,
        request_type="Viagem extra NILO",
        requested_datetime=datetime(2030, 1, 3, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=1,
        vehicle_type_requested="sedan",
        cost_center="CC",
        reason="motivo",
    )
    req_a = create_request(session, partner_a, payload_a)

    # Create travel request for Company B
    partner_b = User(
        full_name="Partner B",
        email="partner_b@test.com",
        role=UserRole.PARTNER_REQUESTER,
        company_id=company_b.id
    )
    session.add(partner_b)
    session.flush()
    payload_b = TravelRequestCreate(
        base_id=base.id,
        request_type="Viagem extra NILO",
        requested_datetime=datetime(2030, 1, 3, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=1,
        vehicle_type_requested="sedan",
        cost_center="CC",
        reason="motivo",
    )
    req_b = create_request(session, partner_b, payload_b)

    # 3. Test legacy fallback (no links): supervisor should be able to see both
    assert supervisor_can_access_request(session, supervisor, req_a) is True
    assert supervisor_can_access_request(session, supervisor, req_b) is True

    # 4. Link supervisor to Company B only
    session.add(UserCompanyBaseLink(user_id=supervisor.id, company_base_id=cb_b.id))
    session.commit()

    # 5. Verify strict scoping: supervisor can see Company B but NOT Company A request
    assert supervisor_can_access_request(session, supervisor, req_b) is True
    assert supervisor_can_access_request(session, supervisor, req_a) is False


def test_cancel_request_requires_reason(session):
    req = _mk_request(session)
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    
    with pytest.raises(DomainError, match="O motivo do cancelamento é obrigatório."):
        cancel_request(session, sup, req, reason=None)
        
    with pytest.raises(DomainError, match="O motivo do cancelamento é obrigatório."):
        cancel_request(session, sup, req, reason="")
        
    with pytest.raises(DomainError, match="O motivo do cancelamento é obrigatório."):
        cancel_request(session, sup, req, reason="   ")


def test_triage_request_partial_or_alternative_requires_observations(session, monkeypatch):
    _mock_email_delivery(monkeypatch)
    req = _mk_request(session)
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    
    from app.models import TriageDecisionPayload
    
    # Try Partial without observations
    payload_partial_empty = TriageDecisionPayload(
        decision_type=DecisionType.PARTIAL,
        approved_quantity=1,
        confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
        confirmed_vehicle_type="sedan",
        tariff_value=0.0,
        observations=None,
    )
    with pytest.raises(DomainError, match="O motivo da decisão \\(observações\\) é obrigatório."):
        triage_request(session, sup, req, payload_partial_empty)
        
    # Try Alternative without observations
    payload_alt_empty = TriageDecisionPayload(
        decision_type=DecisionType.ALTERNATIVE,
        approved_quantity=1,
        confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
        confirmed_vehicle_type="sedan",
        tariff_value=0.0,
        observations="  ",
    )
    with pytest.raises(DomainError, match="O motivo da decisão \\(observações\\) é obrigatório."):
        triage_request(session, sup, req, payload_alt_empty)


def test_triage_request_sends_correct_emails(session, monkeypatch):
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    from app.models import TriageDecisionPayload
    
    sent_emails = []
    def mock_send_email(to_email, subject, body, attachment_path=None):
        sent_emails.append({
            "to_email": to_email,
            "subject": subject,
            "body": body,
            "attachment_path": attachment_path
        })
    monkeypatch.setattr("app.services.workflow.send_email", mock_send_email)

    # 1. Refuse decision should send correct refusal email containing refusal_reason
    req_refuse = _mk_request(session)
    sent_emails.clear()
    payload_refuse = TriageDecisionPayload(
        decision_type=DecisionType.REFUSE,
        approved_quantity=0,
        confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
        confirmed_vehicle_type="sedan",
        tariff_value=0.0,
        refusal_reason="Indisponibilidade de frota",
    )
    triage_request(session, sup, req_refuse, payload_refuse)
    assert len(sent_emails) == 1
    email = sent_emails[-1]
    assert "recusada" in email["subject"].lower()
    assert "Indisponibilidade de frota" in email["body"]
    
    # 2. Partial decision on Quote should send quote email containing decision and reason
    req_quote = _mk_request(session)
    sent_emails.clear()
    req_quote.request_type = "Cotação de preço"
    session.add(req_quote)
    session.commit()
    
    payload_partial = TriageDecisionPayload(
        decision_type=DecisionType.PARTIAL,
        approved_quantity=1,
        confirmed_datetime=datetime(2030, 1, 2, 10, 30, tzinfo=timezone.utc),
        confirmed_vehicle_type="sedan",
        tariff_value=150.0,
        observations="Aprovado apenas 1 veículo devido ao limite",
    )
    triage_request(session, sup, req_quote, payload_partial)
    assert len(sent_emails) == 1
    email = sent_emails[-1]
    assert "Cotação enviada" in email["subject"]
    assert "Parcial" in email["body"]
    assert "Aprovado apenas 1 veículo devido ao limite" in email["body"]


def test_create_request_sends_partner_email(session, monkeypatch):
    sent_emails = []
    def mock_send_email(to_email, subject, body, attachment_path=None):
        sent_emails.append({
            "to_email": to_email,
            "subject": subject,
            "body": body,
            "attachment_path": attachment_path
        })
    monkeypatch.setattr("app.services.workflow.send_email", mock_send_email)

    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    base = session.exec(select(Base)).first()
    
    payload = TravelRequestCreate(
        base_id=base.id,
        request_type="Cotação de preço",
        requested_datetime=datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=1,
        vehicle_type_requested="sedan",
        cost_center="CC",
        reason="motivo",
    )
    
    create_request(session, partner, payload)
    
    partner_emails = [e for e in sent_emails if e["to_email"] == partner.email]
    assert len(partner_emails) == 1
    assert "Solicitação aberta com sucesso" in partner_emails[0]["subject"]
    assert "cotação de preço" in partner_emails[0]["body"]


def test_propose_change_sends_partner_email(session, monkeypatch):
    sent_emails = []
    def mock_send_email(to_email, subject, body, attachment_path=None):
        sent_emails.append({
            "to_email": to_email,
            "subject": subject,
            "body": body,
            "attachment_path": attachment_path
        })
    monkeypatch.setattr("app.services.workflow.send_email", mock_send_email)

    req = _mk_request(session)
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    
    payload = TravelRequestCreate(
        base_id=req.base_id,
        request_type=req.request_type,
        requested_datetime=datetime(2030, 1, 2, 11, 0, tzinfo=timezone.utc),
        origin=req.origin,
        destination=req.destination,
        quantity=req.quantity,
        vehicle_type_requested=req.vehicle_type_requested,
        cost_center=req.cost_center,
        reason=req.reason,
    )
    
    # We clear the list so we only assert emails sent during proposing changes
    sent_emails.clear()
    
    from app.services.workflow import propose_change
    propose_change(session, partner, req, payload)
    
    partner_emails = [e for e in sent_emails if e["to_email"] == partner.email]
    assert len(partner_emails) == 1
    assert "Alteração de solicitação registrada" in partner_emails[0]["subject"]
    assert "status Pendente de Triagem" in partner_emails[0]["body"]


def test_sign_acceptance_sends_supervisor_email(session, monkeypatch):
    sent_emails = []
    def mock_send_email(to_email, subject, body, attachment_path=None):
        sent_emails.append({
            "to_email": to_email,
            "subject": subject,
            "body": body,
            "attachment_path": attachment_path
        })
    monkeypatch.setattr("app.services.workflow.send_email", mock_send_email)

    req = _mk_request(session)
    req.request_type = "Cotação de preço"
    session.add(req)
    session.commit()
    
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    
    from app.models import TriageDecisionPayload
    payload_triage = TriageDecisionPayload(
        decision_type=DecisionType.CONFIRM,
        approved_quantity=1,
        confirmed_datetime=datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc),
        confirmed_vehicle_type="sedan",
        tariff_value=200.0,
    )
    triage_request(session, sup, req, payload_triage)
    assert req.status == RequestStatus.CONFIRMED
    
    from app.services.workflow import request_otp
    otp_code = request_otp(session, partner, req)
    
    # We clear the list so we only assert emails sent during acceptance signing
    sent_emails.clear()
    
    from app.services.workflow import sign_acceptance
    sign_acceptance(session, partner, req, otp_code, "127.0.0.1", "test-agent")
    
    sup_emails = [e for e in sent_emails if e["to_email"] == sup.email]
    assert len(sup_emails) == 1
    assert "Aceite de cotação registrado" in sup_emails[0]["subject"]
    assert "registrou o aceite da proposta" in sup_emails[0]["body"]


def test_dispatch_trip_sends_partner_email(session, monkeypatch):
    sent_emails = []
    def mock_send_email(to_email, subject, body, attachment_path=None):
        sent_emails.append({
            "to_email": to_email,
            "subject": subject,
            "body": body,
            "attachment_path": attachment_path
        })
    monkeypatch.setattr("app.services.workflow.send_email", mock_send_email)

    req = _mk_request(session)
    req.request_type = "Cotação de preço"
    session.add(req)
    session.commit()
    
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    
    from app.models import TriageDecisionPayload
    payload_triage = TriageDecisionPayload(
        decision_type=DecisionType.CONFIRM,
        approved_quantity=1,
        confirmed_datetime=datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc),
        confirmed_vehicle_type="sedan",
        tariff_value=200.0,
    )
    triage_request(session, sup, req, payload_triage)
    
    from app.services.workflow import request_otp, sign_acceptance
    otp_code = request_otp(session, partner, req)
    sign_acceptance(session, partner, req, otp_code, "127.0.0.1", "test-agent")
    assert req.status == RequestStatus.ACCEPTED
    
    driver = session.exec(select(Driver)).first()
    vehicle = session.exec(select(Vehicle)).first()
    
    # We clear the list so we only assert emails sent during dispatch
    sent_emails.clear()
    
    from app.services.workflow import dispatch_trip
    dispatch_trip(session, sup, req, driver.id, vehicle.id, datetime(2030, 1, 2, 10, 15, tzinfo=timezone.utc))
    
    partner_emails = [e for e in sent_emails if e["to_email"] == partner.email]
    assert len(partner_emails) == 1
    assert "Viagem em execução" in partner_emails[0]["subject"]
    assert driver.name in partner_emails[0]["body"]
    assert vehicle.plate in partner_emails[0]["body"]









