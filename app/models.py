from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlmodel import Field, SQLModel, Relationship


class UserRole(str, Enum):
    PARTNER_REQUESTER = "partner_requester"
    BASE_SUPERVISOR = "base_supervisor"
    LOGISTICS_MANAGER = "logistics_manager"
    FINANCE_READONLY = "finance_readonly"


class DriverActivityStatus(str, Enum):
    AVAILABLE = "available"
    IN_ROUTE = "in_route"
    ABSENT = "absent"


class RequestStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    TRIAGE = "triage"
    CONFIRMED = "confirmed"
    ACCEPTED = "accepted"
    IN_EXECUTION = "in_execution"
    COMPLETED = "completed"
    REFUSED = "refused"
    CANCELED = "canceled"


class DecisionType(str, Enum):
    CONFIRM = "confirm"
    PARTIAL = "partial"
    ALTERNATIVE = "alternative"
    REFUSE = "refuse"


class NotificationType(str, Enum):
    REQUEST_SUBMITTED = "request_submitted"
    REQUEST_CONFIRMED = "request_confirmed"
    OTP_SENT = "otp_sent"
    ACCEPTANCE_SIGNED = "acceptance_signed"
    SLA_CRITICAL = "sla_critical"
    TRIP_COMPLETED = "trip_completed"


class Company(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    cnpj: str
    active: bool = True


class UserBaseLink(SQLModel, table=True):
    user_id: int = Field(foreign_key="user.id", primary_key=True)
    base_id: int = Field(foreign_key="base.id", primary_key=True)


class Base(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    location: str
    sla_minutes: int = 30
    min_advance_minutes: int = 120
    active: bool = True
    
    users: List["User"] = Relationship(back_populates="bases", link_model=UserBaseLink)


class CompanyBase(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id")
    base_id: int = Field(foreign_key="base.id")
    contract_sla_minutes: Optional[int] = None


class UserCompanyBaseLink(SQLModel, table=True):
    user_id: int = Field(foreign_key="user.id", primary_key=True)
    company_base_id: int = Field(foreign_key="companybase.id", primary_key=True)


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    full_name: str
    email: str = Field(index=True, unique=True)
    role: UserRole
    company_name: str = "Logtudo"
    phone: Optional[str] = None
    job_title: Optional[str] = None
    address: Optional[str] = None
    company_id: Optional[int] = Field(default=None, foreign_key="company.id")
    base_id: Optional[int] = Field(default=None, foreign_key="base.id")
    password_hash: str = ""
    is_active: bool = True

    bases: List["Base"] = Relationship(back_populates="users", link_model=UserBaseLink)


class Driver(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    phone: str = ""
    base_id: int = Field(foreign_key="base.id")
    vehicle_id: Optional[int] = Field(default=None, foreign_key="vehicle.id")
    active: bool = True
    activity_status: DriverActivityStatus = DriverActivityStatus.AVAILABLE
    status_updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    vehicle: Optional["Vehicle"] = Relationship()


class Vehicle(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    plate: str = Field(index=True, unique=True)
    vehicle_type: str
    base_id: int = Field(foreign_key="base.id")
    active: bool = True


class TravelRequest(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    protocol: str = Field(index=True, unique=True)
    company_id: int = Field(foreign_key="company.id")
    base_id: int = Field(foreign_key="base.id")
    requested_by_user_id: int = Field(foreign_key="user.id")
    request_type: str
    requested_datetime: datetime
    origin: str
    destination: str
    quantity: int
    vehicle_type_requested: str
    cost_center: str
    reason: str
    notes: Optional[str] = None
    status: RequestStatus = RequestStatus.SUBMITTED
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OperationalConfirmation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="travelrequest.id", unique=True)
    supervisor_user_id: int = Field(foreign_key="user.id")
    decision_type: DecisionType
    approved_quantity: int
    confirmed_datetime: datetime
    confirmed_vehicle_type: str
    tariff_value: float
    observations: Optional[str] = None
    driver_id: Optional[int] = Field(default=None, foreign_key="driver.id")
    driver_ids: Optional[str] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class OTPChallenge(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="travelrequest.id")
    user_id: int = Field(foreign_key="user.id")
    code_hash: str
    expires_at: datetime
    consumed_at: Optional[datetime] = None
    resend_count: int = 0
    last_resend_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Acceptance(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="travelrequest.id", unique=True)
    user_id: int = Field(foreign_key="user.id")
    ip_address: str
    user_agent: str
    accepted_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    document_hash: str


class Dispatch(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="travelrequest.id", unique=True)
    driver_id: int = Field(foreign_key="driver.id")
    vehicle_id: int = Field(foreign_key="vehicle.id")
    planned_departure_at: datetime
    actual_departure_at: Optional[datetime] = None
    actual_arrival_at: Optional[datetime] = None
    occurrences: Optional[str] = None


class Document(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: int = Field(foreign_key="travelrequest.id", unique=True)
    file_path: str
    sha256_hash: str
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class Notification(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: Optional[int] = Field(default=None, foreign_key="travelrequest.id")
    user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    type: NotificationType
    channel: str = "email"
    recipient: str
    subject: str
    body: str
    sent_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class EventLog(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    request_id: Optional[int] = Field(default=None, foreign_key="travelrequest.id")
    actor_user_id: Optional[int] = Field(default=None, foreign_key="user.id")
    event_type: str
    payload: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# DTOs
class TravelRequestCreate(SQLModel):
    base_id: int
    request_type: str
    requested_datetime: datetime
    origin: str
    destination: str
    quantity: int
    vehicle_type_requested: str
    cost_center: Optional[str] = None
    reason: Optional[str] = None
    notes: Optional[str] = None


class TriageDecisionPayload(SQLModel):
    decision_type: DecisionType
    approved_quantity: int
    confirmed_datetime: datetime
    confirmed_vehicle_type: str
    tariff_value: float
    observations: Optional[str] = None
    refusal_reason: Optional[str] = None
    driver_id: Optional[int] = None
    driver_ids: Optional[str] = None


class ConfirmationPayload(SQLModel):
    approved_quantity: int
    confirmed_datetime: datetime
    confirmed_vehicle_type: str
    tariff_value: float
    observations: Optional[str] = None


class OTPRequest(SQLModel):
    request_id: int


class OTPVerify(SQLModel):
    request_id: int
    code: str


class DispatchPayload(SQLModel):
    driver_id: int
    vehicle_id: int
    planned_departure_at: datetime


class CompleteTripPayload(SQLModel):
    request_id: int
    actual_departure_at: datetime
    actual_arrival_at: datetime
    occurrences: Optional[str] = None


class BillingFilter(SQLModel):
    base_id: Optional[int] = None
    company_id: Optional[int] = None
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
