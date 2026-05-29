from __future__ import annotations

import csv
import hashlib
import io
import logging
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from sqlmodel import Session, and_, select

from app.auth import hash_password
from app.services.email_sender import EmailDeliveryError, send_email
from app.models import (
    Acceptance,
    Base,
    Company,
    CompanyBase,
    DecisionType,
    Dispatch,
    Document,
    Driver,
    DriverActivityStatus,
    EventLog,
    Notification,
    NotificationType,
    OTPChallenge,
    OperationalConfirmation,
    RequestStatus,
    TriageDecisionPayload,
    TravelRequest,
    TravelRequestCreate,
    User,
    UserRole,
    UserBaseLink,
    Vehicle,
)


class DomainError(Exception):
    pass


logger = logging.getLogger(__name__)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ensure_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def ensure_user_scope(user: User, request: TravelRequest) -> None:
    if user.role == UserRole.PARTNER_REQUESTER and user.company_id != request.company_id:
        raise DomainError("Acesso negado ao pedido de outra empresa.")

    # Supervisor can be scoped by explicit base links or legacy single base_id.
    if user.role == UserRole.BASE_SUPERVISOR:
        allowed_base_ids = {b.id for b in user.bases if b.id is not None}
        if not allowed_base_ids and user.base_id is not None:
            allowed_base_ids = {user.base_id}
        if request.base_id not in allowed_base_ids:
            raise DomainError("Acesso negado ao pedido de outra base.")


OTP_VALID_MINUTES = 5
OTP_RESEND_DELAY_MINUTES = 1
OTP_MAX_RESENDS = 3


def _get_latest_otp_challenge(session: Session, request_id: int, user_id: int) -> OTPChallenge | None:
    return session.exec(
        select(OTPChallenge)
        .where(and_(OTPChallenge.request_id == request_id, OTPChallenge.user_id == user_id))
        .order_by(OTPChallenge.created_at.desc())
    ).first()


def validate_otp_resend_attempts(challenge: OTPChallenge) -> None:
    if challenge.resend_count >= OTP_MAX_RESENDS:
        raise DomainError("Limite de reenvios atingido, solicite um novo código.")
    if challenge.last_resend_at and (now_utc() - ensure_aware(challenge.last_resend_at)) < timedelta(minutes=OTP_RESEND_DELAY_MINUTES):
        raise DomainError("Aguarde um minuto antes de reenviar o OTP.")


def _get_operational_confirmation(session: Session, request_id: int) -> OperationalConfirmation | None:
    return session.exec(
        select(OperationalConfirmation).where(OperationalConfirmation.request_id == request_id)
    ).first()


def create_otp_challenge(
    session: Session,
    user: User,
    request: TravelRequest,
    resend_count: int = 0,
    last_resend_at: Optional[datetime] = None,
) -> str:
    code = f"{random.randint(100000, 999999)}"
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    challenge = OTPChallenge(
        request_id=request.id,
        user_id=user.id,
        code_hash=code_hash,
        expires_at=now_utc() + timedelta(minutes=OTP_VALID_MINUTES),
        resend_count=resend_count,
        last_resend_at=last_resend_at,
    )
    session.add(challenge)
    session.flush()
    try:
        send_email_notification(
            session,
            NotificationType.OTP_SENT,
            user.email,
            f"OTP de aceite do pedido {request.protocol}",
            f"Seu codigo OTP e: {code}. Valido por {OTP_VALID_MINUTES} minutos.",
            request_id=request.id,
            user_id=user.id,
            strict_delivery=True,
        )
    except EmailDeliveryError as exc:
        logger.exception("Falha no envio de OTP para request_id=%s", request.id)
        raise DomainError("Nao foi possivel enviar o OTP agora. Verifique a configuracao de e-mail.") from exc
    log_event(session, request.id, user.id, "otp_sent", "otp_generated")
    session.commit()
    return code


def request_otp(session: Session, user: User, request: TravelRequest) -> str:
    if user.role != UserRole.PARTNER_REQUESTER:
        raise DomainError("Somente parceiro pode solicitar OTP.")
    ensure_user_scope(user, request)
    if request.status != RequestStatus.CONFIRMED:
        raise DomainError("OTP apenas para pedido confirmado.")
    if not _get_operational_confirmation(session, request.id):
        raise DomainError("Confirmacao operacional ausente. Reabra a triagem do pedido.")

    return create_otp_challenge(session, user, request)


def resend_otp(session: Session, user: User, request: TravelRequest) -> str:
    if user.role != UserRole.PARTNER_REQUESTER:
        raise DomainError("Somente parceiro pode solicitar OTP.")
    ensure_user_scope(user, request)
    if request.status != RequestStatus.CONFIRMED:
        raise DomainError("OTP apenas para pedido confirmado.")
    if not _get_operational_confirmation(session, request.id):
        raise DomainError("Confirmacao operacional ausente. Reabra a triagem do pedido.")

    current_challenge = _get_latest_otp_challenge(session, request.id, user.id)
    if current_challenge:
        validate_otp_resend_attempts(current_challenge)
        resend_count = current_challenge.resend_count + 1
        last_resend_at = now_utc()
    else:
        resend_count = 0
        last_resend_at = None

    return create_otp_challenge(
        session,
        user,
        request,
        resend_count=resend_count,
        last_resend_at=last_resend_at,
    )


def log_event(session: Session, request_id: Optional[int], actor_user_id: Optional[int], event_type: str, payload: str) -> None:
    session.add(EventLog(request_id=request_id, actor_user_id=actor_user_id, event_type=event_type, payload=payload))


def send_email_notification(
    session: Session,
    ntype: NotificationType,
    recipient: str,
    subject: str,
    body: str,
    request_id: Optional[int] = None,
    user_id: Optional[int] = None,
    strict_delivery: bool = False,
) -> None:
    session.add(
        Notification(
            request_id=request_id,
            user_id=user_id,
            type=ntype,
            recipient=recipient,
            subject=subject,
            body=body,
        )
    )
    try:
        send_email(recipient, subject, body)
    except EmailDeliveryError:
        if strict_delivery:
            raise
        logger.exception("Falha ao enviar notificacao de e-mail para %s", recipient)


def generate_protocol(session: Session) -> str:
    date_part = now_utc().strftime("%Y%m%d")
    count = session.exec(select(TravelRequest)).all()
    return f"VX-{date_part}-{len(count)+1:04d}"


def create_request(session: Session, user: User, payload: TravelRequestCreate) -> TravelRequest:
    if user.role != UserRole.PARTNER_REQUESTER:
        raise DomainError("Apenas parceiro pode criar solicitação.")

    base = session.get(Base, payload.base_id)
    if not base:
        raise DomainError("Base inválida.")

    allowed = session.exec(
        select(CompanyBase).where(
            and_(CompanyBase.company_id == user.company_id, CompanyBase.base_id == payload.base_id)
        )
    ).first()
    if not allowed:
        raise DomainError("Empresa não autorizada para esta base.")

    minutes_diff = (payload.requested_datetime - now_utc()).total_seconds() / 60
    if minutes_diff < base.min_advance_minutes:
        raise DomainError("Antecedência mínima não atendida para a base.")

    request = TravelRequest(
        protocol=generate_protocol(session),
        company_id=user.company_id,
        base_id=payload.base_id,
        requested_by_user_id=user.id,
        request_type=payload.request_type,
        requested_datetime=payload.requested_datetime,
        origin=payload.origin,
        destination=payload.destination,
        quantity=payload.quantity,
        vehicle_type_requested=payload.vehicle_type_requested,
        cost_center=(payload.cost_center or "").strip(),
        reason=(payload.reason or "").strip(),
        notes=payload.notes,
        status=RequestStatus.SUBMITTED,
    )
    session.add(request)
    session.flush()

    log_event(session, request.id, user.id, "request_submitted", f"protocol={request.protocol}")

    supervisors = session.exec(
        select(User).where(and_(User.role == UserRole.BASE_SUPERVISOR, User.base_id == request.base_id, User.is_active == True))
    ).all()
    for sup in supervisors:
        send_email_notification(
            session,
            NotificationType.REQUEST_SUBMITTED,
            sup.email,
            f"Novo pedido {request.protocol}",
            "Novo pedido submetido para sua base.",
            request_id=request.id,
            user_id=sup.id,
        )

    session.commit()
    session.refresh(request)
    return request


def compute_sla(request: TravelRequest, base: Base, session: Optional[Session] = None) -> tuple[float, str]:
    sla_minutes = base.sla_minutes
    
    if session:
        cb = session.exec(select(CompanyBase).where(
            and_(CompanyBase.company_id == request.company_id, CompanyBase.base_id == request.base_id)
        )).first()
        if cb and cb.contract_sla_minutes:
            sla_minutes = cb.contract_sla_minutes

    # SQLite may return naive datetimes; normalize before subtraction.
    elapsed = (now_utc() - ensure_aware(request.created_at)).total_seconds() / 60
    ratio = elapsed / max(sla_minutes, 1)
    if ratio >= 1:
        return ratio, "red"
    if ratio >= 0.7:
        return ratio, "yellow"
    return ratio, "green"


def triage_request(session: Session, user: User, request: TravelRequest, payload: TriageDecisionPayload):
    if user.role not in (UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER):
        raise DomainError("Perfil sem permissão para triagem.")
    ensure_user_scope(user, request)

    if request.status not in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE, RequestStatus.CONFIRMED):
        raise DomainError("Pedido fora de status para triagem ou edição.")

    request.status = RequestStatus.TRIAGE
    request.updated_at = now_utc()

    if payload.decision_type == DecisionType.REFUSE:
        if not payload.refusal_reason:
            raise DomainError("Motivo obrigatório para recusa.")
        request.status = RequestStatus.REFUSED
        log_event(session, request.id, user.id, "request_refused", payload.refusal_reason)
    else:
        request.status = RequestStatus.CONFIRMED
        
        # Check for existing confirmation to update it
        existing_conf = session.exec(
            select(OperationalConfirmation).where(OperationalConfirmation.request_id == request.id)
        ).first()

        if existing_conf:
            existing_conf.supervisor_user_id = user.id
            existing_conf.decision_type = payload.decision_type
            existing_conf.approved_quantity = payload.approved_quantity
            existing_conf.confirmed_datetime = payload.confirmed_datetime
            existing_conf.confirmed_vehicle_type = payload.confirmed_vehicle_type
            existing_conf.tariff_value = payload.tariff_value
            existing_conf.observations = payload.observations
            session.add(existing_conf)
        else:
            session.add(
                OperationalConfirmation(
                    request_id=request.id,
                    supervisor_user_id=user.id,
                    decision_type=payload.decision_type,
                    approved_quantity=payload.approved_quantity,
                    confirmed_datetime=payload.confirmed_datetime,
                    confirmed_vehicle_type=payload.confirmed_vehicle_type,
                    tariff_value=payload.tariff_value,
                    observations=payload.observations,
                )
            )
        
        log_event(session, request.id, user.id, "request_confirmed", payload.decision_type.value)

        requester = session.get(User, request.requested_by_user_id)
        if requester:
            send_email_notification(
                session,
                NotificationType.REQUEST_CONFIRMED,
                requester.email,
                f"Pedido {request.protocol} confirmado",
                "A operação confirmou o pedido. Faça o aceite no portal.",
                request_id=request.id,
                user_id=requester.id,
            )

    session.add(request)
    session.commit()


def propose_change(session: Session, user: User, request: TravelRequest, payload: TravelRequestCreate):
    if user.role != UserRole.PARTNER_REQUESTER:
        raise DomainError("Apenas parceiro pode propor alteração.")
    ensure_user_scope(user, request)

    if request.status not in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE, RequestStatus.CONFIRMED):
        raise DomainError("Status atual não permite alteração.")

    # Update request details
    request.quantity = payload.quantity
    request.requested_datetime = payload.requested_datetime
    request.vehicle_type_requested = payload.vehicle_type_requested
    request.origin = payload.origin
    request.destination = payload.destination
    request.cost_center = payload.cost_center
    request.reason = payload.reason
    request.notes = payload.notes
    
    # Reset status and clear confirmation
    request.status = RequestStatus.SUBMITTED
    request.updated_at = now_utc()
    
    existing_conf = _get_operational_confirmation(session, request.id)
    if existing_conf:
        session.delete(existing_conf)
    
    log_event(session, request.id, user.id, "request_modified_by_partner", "Partner proposed changes, resetting triage.")
    session.add(request)
    session.commit()


def sign_acceptance(session: Session, user: User, request: TravelRequest, code: str, ip: str, user_agent: str) -> Acceptance:
    if user.role != UserRole.PARTNER_REQUESTER:
        raise DomainError("Somente parceiro pode assinar.")
    ensure_user_scope(user, request)
    if request.status != RequestStatus.CONFIRMED:
        raise DomainError("Aceite somente para pedido confirmado.")

    challenge = session.exec(
        select(OTPChallenge)
        .where(and_(OTPChallenge.request_id == request.id, OTPChallenge.user_id == user.id))
        .order_by(OTPChallenge.created_at.desc())
    ).first()

    if not challenge:
        raise DomainError("OTP nao encontrado.")
    if challenge.consumed_at is not None:
        raise DomainError("OTP ja utilizado.")
    if ensure_aware(challenge.expires_at) < now_utc():
        raise DomainError("OTP expirado.")

    incoming_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if incoming_hash != challenge.code_hash:
        raise DomainError("OTP invalido.")

    confirmation = _get_operational_confirmation(session, request.id)
    if not confirmation:
        raise DomainError("Confirmacao operacional ausente.")

    frozen_summary = (
        f"protocol={request.protocol}|qty={confirmation.approved_quantity}|"
        f"datetime={confirmation.confirmed_datetime.isoformat()}|vehicle={confirmation.confirmed_vehicle_type}|"
        f"tariff={confirmation.tariff_value}"
    )
    doc_hash = hashlib.sha256(frozen_summary.encode("utf-8")).hexdigest()

    acceptance = Acceptance(
        request_id=request.id,
        user_id=user.id,
        ip_address=ip,
        user_agent=user_agent[:255],
        document_hash=doc_hash,
    )
    session.add(acceptance)
    challenge.consumed_at = now_utc()
    request.status = RequestStatus.ACCEPTED
    request.updated_at = now_utc()

    log_event(session, request.id, user.id, "acceptance_signed", f"hash={doc_hash}")
    send_email_notification(
        session,
        NotificationType.ACCEPTANCE_SIGNED,
        user.email,
        f"Aceite registrado {request.protocol}",
        "Seu aceite foi registrado com sucesso.",
        request_id=request.id,
        user_id=user.id,
    )

    session.add(challenge)
    session.add(request)
    session.commit()
    session.refresh(acceptance)
    return acceptance


def can_partner_cancel_request(session: Session, request: TravelRequest) -> bool:
    if request.status in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE):
        return True
    if request.status != RequestStatus.CONFIRMED:
        return False

    confirmation = _get_operational_confirmation(session, request.id)
    if not confirmation:
        return False

    cancel_deadline = ensure_aware(confirmation.confirmed_datetime) - timedelta(hours=24)
    return now_utc() <= cancel_deadline


def cancel_request(session: Session, user: User, request: TravelRequest, reason: str | None = None) -> TravelRequest:
    if user.role != UserRole.PARTNER_REQUESTER:
        raise DomainError("Somente parceiro pode cancelar.")
    ensure_user_scope(user, request)

    if request.status in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE):
        pass
    elif request.status == RequestStatus.CONFIRMED:
        confirmation = _get_operational_confirmation(session, request.id)
        if not confirmation:
            raise DomainError("Confirmacao operacional ausente. Reabra a triagem do pedido.")
        cancel_deadline = ensure_aware(confirmation.confirmed_datetime) - timedelta(hours=24)
        if now_utc() > cancel_deadline:
            raise DomainError("Cancelamento indisponivel. Prazo contratual de 24h antes da viagem confirmado foi excedido.")
    elif request.status == RequestStatus.CANCELED:
        raise DomainError("Pedido ja cancelado.")
    else:
        raise DomainError("Cancelamento indisponivel para o status atual.")

    request.status = RequestStatus.CANCELED
    request.updated_at = now_utc()
    session.add(request)
    log_event(session, request.id, user.id, "request_canceled", reason or "")
    session.commit()
    session.refresh(request)
    return request


def dispatch_trip(session: Session, user: User, request: TravelRequest, driver_id: int, vehicle_id: int, planned_departure_at: datetime) -> Dispatch:
    if user.role not in (UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER):
        raise DomainError("Perfil sem permissao de despacho.")
    ensure_user_scope(user, request)
    if request.status != RequestStatus.ACCEPTED:
        raise DomainError("Despacho exige aceite assinado.")

    driver = session.get(Driver, driver_id)
    vehicle = session.get(Vehicle, vehicle_id)
    if not driver or not vehicle:
        raise DomainError("Motorista ou veiculo invalido.")
    if driver.base_id != request.base_id or vehicle.base_id != request.base_id:
        raise DomainError("Motorista/veiculo fora da base do pedido.")
    if driver.activity_status == DriverActivityStatus.ABSENT:
        raise DomainError("Motorista ausente nao pode ser alocado.")
    if driver.activity_status == DriverActivityStatus.IN_ROUTE:
        raise DomainError("Motorista ja esta em rota.")

    dispatch = Dispatch(
        request_id=request.id,
        driver_id=driver_id,
        vehicle_id=vehicle_id,
        planned_departure_at=planned_departure_at,
    )
    request.status = RequestStatus.IN_EXECUTION
    request.updated_at = now_utc()
    driver.activity_status = DriverActivityStatus.IN_ROUTE
    driver.status_updated_at = now_utc()

    session.add(dispatch)
    session.add(request)
    session.add(driver)
    log_event(session, request.id, user.id, "trip_dispatched", f"driver={driver_id}|vehicle={vehicle_id}")
    session.commit()
    session.refresh(dispatch)
    return dispatch


def generate_pdf_document(session: Session, request: TravelRequest) -> Document:
    base_dir = Path(__file__).resolve().parent.parent.parent
    docs_dir = base_dir / "app" / "data" / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    file_path = docs_dir / f"{request.protocol}.pdf"

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Comprovante {request.protocol}")
    pdf.drawString(50, 800, "Central de Viagens Extras - Comprovante")
    pdf.drawString(50, 780, f"Protocolo: {request.protocol}")
    pdf.drawString(50, 760, f"Status: {request.status.value}")

    events = session.exec(select(EventLog).where(EventLog.request_id == request.id).order_by(EventLog.created_at)).all()
    y = 730
    for event in events[:20]:
        pdf.drawString(50, y, f"{event.created_at.isoformat()} - {event.event_type} - {event.payload[:90]}")
        y -= 16
        if y < 70:
            pdf.showPage()
            y = 800

    pdf.save()
    data = buffer.getvalue()
    file_path.write_bytes(data)

    digest = hashlib.sha256(data).hexdigest()
    existing = session.exec(select(Document).where(Document.request_id == request.id)).first()
    if existing:
        existing.file_path = str(file_path)
        existing.sha256_hash = digest
        session.add(existing)
        session.commit()
        return existing

    document = Document(request_id=request.id, file_path=str(file_path), sha256_hash=digest)
    session.add(document)
    session.commit()
    session.refresh(document)
    return document


def complete_trip(
    session: Session,
    user: User,
    request: TravelRequest,
    actual_departure_at: datetime,
    actual_arrival_at: datetime,
    occurrences: Optional[str],
) -> Document:
    if user.role not in (UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER):
        raise DomainError("Perfil sem permissao de conclusao.")
    ensure_user_scope(user, request)
    if request.status != RequestStatus.IN_EXECUTION:
        raise DomainError("A viagem precisa estar em execucao.")

    dispatch = session.exec(select(Dispatch).where(Dispatch.request_id == request.id)).first()
    if not dispatch:
        raise DomainError("Despacho ausente.")

    dispatch.actual_departure_at = actual_departure_at
    dispatch.actual_arrival_at = actual_arrival_at
    dispatch.occurrences = occurrences
    request.status = RequestStatus.COMPLETED
    request.updated_at = now_utc()
    driver = session.get(Driver, dispatch.driver_id)
    if driver:
        driver.activity_status = DriverActivityStatus.AVAILABLE
        driver.status_updated_at = now_utc()

    session.add(dispatch)
    session.add(request)
    if driver:
        session.add(driver)
    log_event(session, request.id, user.id, "trip_completed", f"arrival={actual_arrival_at.isoformat()}")

    document = generate_pdf_document(session, request)

    requester = session.get(User, request.requested_by_user_id)
    if requester:
        send_email_notification(
            session,
            NotificationType.TRIP_COMPLETED,
            requester.email,
            f"Viagem concluida {request.protocol}",
            "Comprovante disponivel no portal.",
            request_id=request.id,
            user_id=requester.id,
        )

    session.commit()
    return document


def list_billable_requests(session: Session, company_id: Optional[int], base_id: Optional[int]):
    stmt = select(TravelRequest).where(TravelRequest.status == RequestStatus.COMPLETED)
    if company_id:
        stmt = stmt.where(TravelRequest.company_id == company_id)
    if base_id:
        stmt = stmt.where(TravelRequest.base_id == base_id)

    requests = session.exec(stmt).all()
    valid = []
    for req in requests:
        acceptance = session.exec(select(Acceptance).where(Acceptance.request_id == req.id)).first()
        if acceptance:
            valid.append(req)
    return valid


def billing_csv(session: Session, company_id: Optional[int], base_id: Optional[int]) -> str:
    rows = list_billable_requests(session, company_id=company_id, base_id=base_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["protocol", "company_id", "base_id", "status", "requested_datetime"])
    for row in rows:
        writer.writerow([row.protocol, row.company_id, row.base_id, row.status.value, row.requested_datetime.isoformat()])
    return output.getvalue()


def seed_data(session: Session) -> None:
    default_password_by_role = {
        UserRole.PARTNER_REQUESTER: "parceiro123",
        UserRole.BASE_SUPERVISOR: "supervisor123",
        UserRole.LOGISTICS_MANAGER: "gerente123",
        UserRole.FINANCE_READONLY: "financeiro123",
    }

    def ensure_partner_all_bases() -> None:
        partner = session.exec(select(User).where(User.email == "parceiro@logtudo.local")).first()
        if not partner:
            company = session.exec(select(Company).where(Company.name == "Parceiro Exemplo")).first()
            if not company:
                company = Company(name="Parceiro Exemplo", cnpj="12.345.678/0001-90")
                session.add(company)
                session.flush()
            partner = User(
                full_name="Ana Parceira",
                email="parceiro@logtudo.local",
                role=UserRole.PARTNER_REQUESTER,
                company_name=company.name,
                company_id=company.id,
                password_hash=hash_password(default_password_by_role[UserRole.PARTNER_REQUESTER]),
            )
            session.add(partner)
            session.flush()

        bases = session.exec(select(Base)).all()
        existing_links = session.exec(select(CompanyBase).where(CompanyBase.company_id == partner.company_id)).all()
        linked_base_ids = {cb.base_id for cb in existing_links}
        created_link = False
        for base in bases:
            if base.id not in linked_base_ids:
                session.add(CompanyBase(company_id=partner.company_id, base_id=base.id, contract_sla_minutes=30))
                created_link = True
        if created_link:
            session.commit()

    if session.exec(select(User)).first():
        users = session.exec(select(User)).all()
        changed = False
        for user in users:
            if not user.password_hash:
                pwd = default_password_by_role.get(user.role, "logtudo123")
                user.password_hash = hash_password(pwd)
                changed = True
            if not user.company_name:
                user.company_name = "Logtudo"
                changed = True
            
            # Ensure supervisor has at least its own base link if many-to-many is empty
            if user.role == UserRole.BASE_SUPERVISOR and not user.bases and user.base_id:
                session.add(UserBaseLink(user_id=user.id, base_id=user.base_id))
                changed = True

            session.add(user)
        if changed:
            session.commit()
        ensure_partner_all_bases()

        # Ensure default managerial users exist even when the DB is not empty
        # This helps test environments where some users were created but others are missing.
        def ensure_user_by_email(email: str, role: UserRole, defaults: dict):
            u = session.exec(select(User).where(User.email == email)).first()
            if not u:
                new = User(email=email, role=role, full_name=defaults.get("full_name", email), company_name=defaults.get("company_name", "Logtudo"), password_hash=hash_password(default_password_by_role.get(role, "logtudo123")))
                # attach optional base_id if provided
                if "base_id" in defaults and defaults["base_id"]:
                    new.base_id = defaults["base_id"]
                session.add(new)
                session.flush()
                # if supervisor and base link model is used, add UserBaseLink
                if role == UserRole.BASE_SUPERVISOR and "base_id" in defaults and defaults["base_id"]:
                    session.add(UserBaseLink(user_id=new.id, base_id=defaults["base_id"]))

        # pick an existing base if available to attach the supervisor
        first_base = session.exec(select(Base)).first()
        ensure_user_by_email(
            "parceiro@logtudo.local",
            UserRole.PARTNER_REQUESTER,
            {"full_name": "Ana Parceira", "company_name": "Parceiro Exemplo"},
        )
        ensure_user_by_email(
            "supervisor@logtudo.local",
            UserRole.BASE_SUPERVISOR,
            {"full_name": "Bruno Supervisor", "company_name": "Logtudo", "base_id": first_base.id if first_base else None},
        )
        ensure_user_by_email(
            "gerente@logtudo.local",
            UserRole.LOGISTICS_MANAGER,
            {"full_name": "Carla Gerente", "company_name": "Logtudo"},
        )
        ensure_user_by_email(
            "financeiro@logtudo.local",
            UserRole.FINANCE_READONLY,
            {"full_name": "Diego Financeiro", "company_name": "Logtudo"},
        )

        session.commit()
        return

    company = Company(name="Parceiro Exemplo", cnpj="12.345.678/0001-90")
    base = Base(name="Base Salvador", location="Salvador", sla_minutes=30, min_advance_minutes=60)
    session.add(company)
    session.add(base)
    session.flush()

    session.add(CompanyBase(company_id=company.id, base_id=base.id, contract_sla_minutes=30))

    users = [
        User(
            full_name="Ana Parceira",
            email="parceiro@logtudo.local",
            role=UserRole.PARTNER_REQUESTER,
            company_name=company.name,
            company_id=company.id,
        ),
        User(
            full_name="Bruno Supervisor",
            email="supervisor@logtudo.local",
            role=UserRole.BASE_SUPERVISOR,
            company_name="Logtudo",
            base_id=base.id,
        ),
        User(
            full_name="Carla Gerente",
            email="gerente@logtudo.local",
            role=UserRole.LOGISTICS_MANAGER,
            company_name="Logtudo",
        ),
        User(
            full_name="Diego Financeiro",
            email="financeiro@logtudo.local",
            role=UserRole.FINANCE_READONLY,
            company_name="Logtudo",
        ),
    ]
    for u in users:
        pwd = default_password_by_role.get(u.role, "logtudo123")
        u.password_hash = hash_password(pwd)
        session.add(u)
    session.flush()

    # Link supervisor
    for u in users:
        if u.role == UserRole.BASE_SUPERVISOR:
            session.add(UserBaseLink(user_id=u.id, base_id=base.id))

    drivers = [
        Driver(name="Motorista 1", phone="71999990001", base_id=base.id),
        Driver(name="Motorista 2", phone="71999990002", base_id=base.id),
    ]
    vehicles = [
        Vehicle(plate="ABC1D23", vehicle_type="sedan", base_id=base.id),
        Vehicle(plate="XYZ9K88", vehicle_type="van", base_id=base.id),
    ]
    for d in drivers:
        session.add(d)
    for v in vehicles:
        session.add(v)

    session.commit()
    ensure_partner_all_bases()
