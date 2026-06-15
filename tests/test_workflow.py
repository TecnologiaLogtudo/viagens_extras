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
        request_type="extra",
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
    payload = TravelRequestCreate(
        base_id=base.id,
        request_type="extra",
        requested_datetime=datetime(2020, 1, 2, 10, 0, tzinfo=timezone.utc),
        origin="A",
        destination="B",
        quantity=1,
        vehicle_type_requested="sedan",
        cost_center="CC",
        reason="motivo",
    )
    with pytest.raises(DomainError):
        create_request(session, partner, payload)


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
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    canceled = cancel_request(session, partner, req, reason="mudanca")
    assert canceled.status == RequestStatus.CANCELED

    req2 = _mk_request(session)
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    req2.status = RequestStatus.TRIAGE
    session.add(req2)
    session.commit()
    canceled2 = cancel_request(session, partner, req2)
    assert canceled2.status == RequestStatus.CANCELED


def test_cancel_allowed_for_confirmed_with_24h_window(session, monkeypatch):
    _mock_email_delivery(monkeypatch)
    req = _mk_request(session)
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
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
    assert can_partner_cancel_request(session, req) is True
    canceled = cancel_request(session, partner, req)
    assert canceled.status == RequestStatus.CANCELED


def test_cancel_blocked_after_confirmed_window(session, monkeypatch):
    _mock_email_delivery(monkeypatch)
    req = _mk_request(session)
    sup = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).first()
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    triage_request(
        session,
        sup,
        req,
        __import__("app.models", fromlist=["TriageDecisionPayload"]).TriageDecisionPayload(
            decision_type=DecisionType.CONFIRM,
            approved_quantity=1,
            confirmed_datetime=datetime(2020, 1, 2, 10, 30, tzinfo=timezone.utc),
            confirmed_vehicle_type="sedan",
            tariff_value=120.0,
        ),
    )
    req = session.get(type(req), req.id)
    assert can_partner_cancel_request(session, req) is False
    with pytest.raises(DomainError, match="Prazo contratual de 24h"):
        cancel_request(session, partner, req)


def test_cancel_blocked_for_other_statuses(session):
    req = _mk_request(session)
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    for status in (
        RequestStatus.ACCEPTED,
        RequestStatus.IN_EXECUTION,
        RequestStatus.COMPLETED,
        RequestStatus.REFUSED,
        RequestStatus.CANCELED,
    ):
        req.status = status
        session.add(req)
        session.commit()
        with pytest.raises(DomainError):
            cancel_request(session, partner, req)


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


