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

    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    code = request_otp(session, partner, req)
    sign_acceptance(session, partner, req, code, "127.0.0.1", "pytest")

    driver = session.exec(select(Driver)).first()
    vehicle = session.exec(select(Vehicle)).first()
    dispatch_trip(session, sup, req, driver.id, vehicle.id, datetime(2030, 1, 2, 10, 40, tzinfo=timezone.utc))
    driver = session.get(Driver, driver.id)
    assert driver.activity_status == DriverActivityStatus.IN_ROUTE
    complete_trip(
        session,
        sup,
        req,
        datetime(2030, 1, 2, 10, 45, tzinfo=timezone.utc),
        datetime(2030, 1, 2, 11, 45, tzinfo=timezone.utc),
        None,
    )

    req = session.get(type(req), req.id)
    assert req.status == RequestStatus.COMPLETED
    driver = session.get(Driver, driver.id)
    assert driver.activity_status == DriverActivityStatus.AVAILABLE
    billable = list_billable_requests(session, None, None)
    assert len(billable) == 1


def test_cannot_dispatch_without_acceptance(session):
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
    driver = session.exec(select(Driver)).first()
    vehicle = session.exec(select(Vehicle)).first()

    with pytest.raises(DomainError):
        dispatch_trip(session, sup, req, driver.id, vehicle.id, datetime(2030, 1, 2, 10, 40, tzinfo=timezone.utc))


def test_cannot_request_otp_without_operational_confirmation(session):
    req = _mk_request(session)
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    req.status = RequestStatus.CONFIRMED
    session.add(req)
    session.commit()

    with pytest.raises(DomainError, match="Confirmacao operacional ausente"):
        request_otp(session, partner, req)


def test_cannot_dispatch_absent_driver(session, monkeypatch):
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
    partner = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).first()
    code = request_otp(session, partner, req)
    sign_acceptance(session, partner, req, code, "127.0.0.1", "pytest")

    driver = session.exec(select(Driver)).first()
    driver.activity_status = DriverActivityStatus.ABSENT
    session.add(driver)
    session.commit()
    vehicle = session.exec(select(Vehicle)).first()

    with pytest.raises(DomainError):
        dispatch_trip(session, sup, req, driver.id, vehicle.id, datetime(2030, 1, 2, 10, 40, tzinfo=timezone.utc))


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
