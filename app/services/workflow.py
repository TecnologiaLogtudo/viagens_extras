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

from app.services.email_sender import EmailDeliveryError, send_email
from app.services.bootstrap import seed_runtime_data
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
    UserCompanyBaseLink,
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


def ensure_user_scope(session: Session, user: User, request: TravelRequest) -> None:
    if user.role == UserRole.PARTNER_REQUESTER and user.company_id != request.company_id:
        raise DomainError("Acesso negado ao pedido de outra empresa.")

    # Supervisor access is scoped by company-base links.
    if user.role == UserRole.BASE_SUPERVISOR:
        allowed = session.exec(
            select(CompanyBase)
            .join(UserCompanyBaseLink, UserCompanyBaseLink.company_base_id == CompanyBase.id)
            .where(
                UserCompanyBaseLink.user_id == user.id,
                CompanyBase.company_id == request.company_id,
                CompanyBase.base_id == request.base_id,
            )
        ).first()
        if not allowed:
            # Fallback to legacy base-only scoping if no specific company-base link exists
            legacy_allowed = False
            if user.base_id == request.base_id:
                legacy_allowed = True
            else:
                user_base_ids = {b.id for b in user.bases if b.id is not None}
                if request.base_id in user_base_ids:
                    legacy_allowed = True
            
            if not legacy_allowed:
                raise DomainError("Acesso negado ao pedido de outra base.")


def supervisor_allowed_base_ids(session: Session, user: User) -> set[int]:
    if user.role != UserRole.BASE_SUPERVISOR:
        return set()
    rows = session.exec(
        select(CompanyBase.base_id)
        .join(UserCompanyBaseLink, UserCompanyBaseLink.company_base_id == CompanyBase.id)
        .where(UserCompanyBaseLink.user_id == user.id)
    ).all()
    allowed_base_ids = set(rows)
    if allowed_base_ids:
        return allowed_base_ids
    legacy_base_ids = {b.id for b in user.bases if b.id is not None}
    if not legacy_base_ids and user.base_id is not None:
        legacy_base_ids = {user.base_id}
    return legacy_base_ids


def supervisor_can_access_request(session: Session, user: User, request: TravelRequest) -> bool:
    if user.role == UserRole.LOGISTICS_MANAGER:
        return True
    if user.role != UserRole.BASE_SUPERVISOR:
        return False
    
    allowed = session.exec(
        select(CompanyBase.id)
        .join(UserCompanyBaseLink, UserCompanyBaseLink.company_base_id == CompanyBase.id)
        .where(
            UserCompanyBaseLink.user_id == user.id,
            CompanyBase.company_id == request.company_id,
            CompanyBase.base_id == request.base_id,
        )
    ).first() is not None

    if allowed:
        return True
    
    # Fallback for legacy scoping
    if user.base_id == request.base_id:
        return True
    
    user_base_ids = {b.id for b in user.bases if b.id is not None}
    if request.base_id in user_base_ids:
        return True

    return False


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
    ensure_user_scope(session, user, request)
    if request.status != RequestStatus.CONFIRMED:
        raise DomainError("OTP apenas para pedido confirmado.")
    if not _get_operational_confirmation(session, request.id):
        raise DomainError("Confirmacao operacional ausente. Reabra a triagem do pedido.")

    return create_otp_challenge(session, user, request)


def resend_otp(session: Session, user: User, request: TravelRequest) -> str:
    if user.role != UserRole.PARTNER_REQUESTER:
        raise DomainError("Somente parceiro pode solicitar OTP.")
    ensure_user_scope(session, user, request)
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

    req_vts = [v.strip() for v in payload.vehicle_type_requested.split(",") if v.strip()]
    if len(req_vts) != payload.quantity:
        raise DomainError("A quantidade de tipos de veículos solicitados deve ser igual à quantidade total de veículos.")

    allowed = session.exec(
        select(CompanyBase).where(
            and_(CompanyBase.company_id == user.company_id, CompanyBase.base_id == payload.base_id)
        )
    ).first()
    if not allowed:
        raise DomainError("Empresa não autorizada para esta base.")

    if payload.requested_datetime <= now_utc():
        raise DomainError("A data/hora do carregamento deve ser no futuro.")

    min_advance = getattr(user, "min_advance_minutes", 0) or 0
    if min_advance > 0:
        minutes_diff = (payload.requested_datetime - now_utc()).total_seconds() / 60
        if minutes_diff < min_advance:
            raise DomainError(f"Antecedência mínima não atendida. Este parceiro exige antecedência mínima de {min_advance} minutos.")

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
        select(User)
        .join(UserCompanyBaseLink, UserCompanyBaseLink.user_id == User.id)
        .join(CompanyBase, CompanyBase.id == UserCompanyBaseLink.company_base_id)
        .where(
            and_(
                User.role == UserRole.BASE_SUPERVISOR,
                User.is_active == True,
                CompanyBase.company_id == request.company_id,
                CompanyBase.base_id == request.base_id,
            )
        )
        .distinct()
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
    ensure_user_scope(session, user, request)

    if request.status not in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE, RequestStatus.CONFIRMED, RequestStatus.COMPLETED):
        raise DomainError("Pedido fora de status para triagem ou edição.")

    request.status = RequestStatus.TRIAGE
    request.updated_at = now_utc()

    if payload.decision_type == DecisionType.REFUSE:
        if not payload.refusal_reason:
            raise DomainError("Motivo obrigatório para recusa.")
        request.status = RequestStatus.REFUSED
        log_event(session, request.id, user.id, "request_refused", payload.refusal_reason)
    else:
        # Validate confirmed vehicle types
        vts = [v.strip() for v in payload.confirmed_vehicle_type.split(",") if v.strip()]
        if len(vts) != payload.approved_quantity:
            raise DomainError("A quantidade de tipos de veículos confirmados deve ser igual à quantidade aprovada.")

        # Validate driver_ids
        driver_ids_list = []
        if payload.driver_ids:
            driver_ids_list = [d.strip() for d in payload.driver_ids.split(",") if d.strip()]
        elif payload.driver_id:
            driver_ids_list = [str(payload.driver_id)]
            payload.driver_ids = str(payload.driver_id)
        
        first_driver_id = None
        if driver_ids_list:
            if len(driver_ids_list) != payload.approved_quantity:
                raise DomainError("A quantidade de motoristas vinculados deve ser igual à quantidade aprovada.")
                
            for d_id_str in driver_ids_list:
                if not d_id_str.isdigit():
                    raise DomainError(f"ID do motorista inválido: {d_id_str}")
                d_id = int(d_id_str)
                d = session.get(Driver, d_id)
                if not d:
                    raise DomainError(f"Motorista com ID {d_id} não encontrado.")
                if d.base_id != request.base_id:
                    raise DomainError(f"Motorista {d.name} não pertence à base do pedido.")
            first_driver_id = int(driver_ids_list[0])

        request.status = RequestStatus.COMPLETED
        
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
            existing_conf.driver_id = first_driver_id
            existing_conf.driver_ids = payload.driver_ids
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
                    driver_id=first_driver_id,
                    driver_ids=payload.driver_ids,
                )
            )
        
        log_event(session, request.id, user.id, "request_confirmed", payload.decision_type.value)

        requester = session.get(User, request.requested_by_user_id)
        if requester:
            send_email_notification(
                session,
                NotificationType.REQUEST_CONFIRMED,
                requester.email,
                f"Pedido {request.protocol} concluído",
                "A operação confirmou e concluiu o pedido. O comprovante está disponível no portal.",
                request_id=request.id,
                user_id=requester.id,
            )

    session.add(request)
    if payload.decision_type != DecisionType.REFUSE:
        session.flush()
        generate_pdf_document(session, request)
    session.commit()


def propose_change(session: Session, user: User, request: TravelRequest, payload: TravelRequestCreate):
    if user.role != UserRole.PARTNER_REQUESTER:
        raise DomainError("Apenas parceiro pode propor alteração.")
    ensure_user_scope(session, user, request)

    if request.status not in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE, RequestStatus.CONFIRMED):
        raise DomainError("Status atual não permite alteração.")

    req_vts = [v.strip() for v in payload.vehicle_type_requested.split(",") if v.strip()]
    if len(req_vts) != payload.quantity:
        raise DomainError("A quantidade de tipos de veículos solicitados deve ser igual à quantidade total de veículos.")

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
    ensure_user_scope(session, user, request)
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
    if request.status not in (RequestStatus.CONFIRMED, RequestStatus.COMPLETED):
        return False

    confirmation = _get_operational_confirmation(session, request.id)
    if not confirmation:
        return False

    cancel_deadline = ensure_aware(confirmation.confirmed_datetime) - timedelta(hours=24)
    return now_utc() <= cancel_deadline


def cancel_request(session: Session, user: User, request: TravelRequest, reason: str | None = None) -> TravelRequest:
    if user.role != UserRole.PARTNER_REQUESTER:
        raise DomainError("Somente parceiro pode cancelar.")
    ensure_user_scope(session, user, request)

    if request.status in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE):
        pass
    elif request.status in (RequestStatus.CONFIRMED, RequestStatus.COMPLETED):
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
    ensure_user_scope(session, user, request)
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

    # Set all other pre-selected drivers to IN_ROUTE
    conf = session.exec(
        select(OperationalConfirmation).where(OperationalConfirmation.request_id == request.id)
    ).first()
    if conf and conf.driver_ids:
        driver_ids_list = [int(d.strip()) for d in conf.driver_ids.split(",") if d.strip().isdigit()]
        for d_id in driver_ids_list:
            if d_id != driver_id:
                other_driver = session.get(Driver, d_id)
                if other_driver:
                    other_driver.activity_status = DriverActivityStatus.IN_ROUTE
                    other_driver.status_updated_at = now_utc()
                    session.add(other_driver)

    log_event(session, request.id, user.id, "trip_dispatched", f"driver={driver_id}|vehicle={vehicle_id}")
    session.commit()
    session.refresh(dispatch)
    return dispatch


def generate_pdf_document(session: Session, request: TravelRequest) -> Document:
    """Generate a highly polished, professional PDF receipt for a completed trip.

    Replicates the format and content of the Agendamento.docx template.
    """
    from reportlab.lib.colors import HexColor
    from reportlab.platypus import Table, TableStyle, Paragraph
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    import re

    base_dir = Path(__file__).resolve().parent.parent.parent
    docs_dir = base_dir / "app" / "data" / "documents"
    docs_dir.mkdir(parents=True, exist_ok=True)
    file_path = docs_dir / f"{request.protocol}.pdf"

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setTitle(f"Comprovante {request.protocol}")

    # --- Header Texts ---
    pdf.setFont("Helvetica-Bold", 20)
    pdf.setFillColor(HexColor("#0f172a"))
    pdf.drawString(50, 775, "Agendamento de Veículos")

    pdf.setFont("Helvetica", 10)
    pdf.setFillColor(HexColor("#475569"))
    pdf.drawString(50, 760, "Logtudo soluções logísticas")

    pdf.setFont("Helvetica-Bold", 14)
    pdf.setFillColor(HexColor("#0f172a"))
    pdf.drawCentredString(297.6, 725, "Comprovante de Agendamento de Transporte")

    # Thin decorative line below header
    pdf.setStrokeColor(HexColor("#cbd5e1"))
    pdf.setLineWidth(1)
    pdf.line(50, 745, 545, 745)

    # Draw logo on the right side if it exists
    logo_path = base_dir / "app" / "static" / "imagens" / "LogoPrincipal.png"
    if logo_path.exists():
        pdf.drawImage(str(logo_path), 505, 755, width=40, height=40, mask='auto', preserveAspectRatio=True)

    # --- Data Retrieval ---
    # Supplier
    company = session.get(Company, request.company_id)
    
    # Base
    base = session.get(Base, request.base_id)
    
    # Operational Confirmation
    conf = session.exec(select(OperationalConfirmation).where(OperationalConfirmation.request_id == request.id)).first()
    
    # Acceptance & Signer (fallback for legacy requests)
    acceptance = session.exec(select(Acceptance).where(Acceptance.request_id == request.id)).first()
    if acceptance:
        signer = session.get(User, acceptance.user_id)
        approval_date_str = acceptance.accepted_at.strftime("%d/%m/%Y %H:%M")
        approval_method = "OTP (One-Time Password)"
    elif conf:
        signer = session.get(User, conf.supervisor_user_id)
        approval_date_str = conf.created_at.strftime("%d/%m/%Y %H:%M") if conf.created_at else "N/A"
        approval_method = "Confirmação do Supervisor"
    else:
        signer = None
        approval_date_str = "N/A"
        approval_method = "Confirmação do Supervisor"

    # OTP Code extraction
    otp_code = "N/A"
    notification = session.exec(
        select(Notification)
        .where(
            Notification.request_id == request.id,
            Notification.type == NotificationType.OTP_SENT
        )
        .order_by(Notification.sent_at.desc())
    ).first()
    if notification:
        match = re.search(r"codigo OTP e:\s*(\d+)", notification.body)
        if match:
            otp_code = match.group(1)

    # --- Styles ---
    styles = getSampleStyleSheet()
    
    normal_style = ParagraphStyle(
        'ValStyle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=11,
        textColor=HexColor("#0f172a")
    )
    
    bold_style = ParagraphStyle(
        'LabelStyle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=HexColor("#334155")
    )
    
    header_style = ParagraphStyle(
        'HeaderStyle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10,
        leading=12,
        textColor=HexColor("#0f172a")
    )

    # --- Table Data ---
    data = [
        [Paragraph("1. Dados do Fornecedor (Solicitante)", header_style), ""],
        [Paragraph("CNPJ do Fornecedor:", bold_style), Paragraph(company.cnpj if company else "N/A", normal_style)],
        [Paragraph("Nome do Fornecedor:", bold_style), Paragraph(company.name if company else "N/A", normal_style)],
        
        [Paragraph("2. Dados da Solicitação", header_style), ""],
        [Paragraph("Data de Agendamento:", bold_style), Paragraph(request.requested_datetime.strftime("%d/%m/%Y") if request.requested_datetime else "N/A", normal_style)],
        [Paragraph("Horário:", bold_style), Paragraph(request.requested_datetime.strftime("%H:%M") if request.requested_datetime else "N/A", normal_style)],
        [Paragraph("Base:", bold_style), Paragraph(f"{base.name} - {base.location}" if base else "N/A", normal_style)],
        [Paragraph("Tipo de pedido:", bold_style), Paragraph("Viagem extra", normal_style)],
        [Paragraph("Nº do Pedido:", bold_style), Paragraph(request.protocol, normal_style)],
        [Paragraph("Qtd. Veículos:", bold_style), Paragraph(str(request.quantity), normal_style)],
        [Paragraph("Tipo de Veículos:", bold_style), Paragraph(request.vehicle_type_requested, normal_style)],
        [Paragraph("Origem:", bold_style), Paragraph(request.origin, normal_style)],
        [Paragraph("Destino:", bold_style), Paragraph(request.destination, normal_style)],
        
        [Paragraph("3. Aprovação", header_style), ""],
        [Paragraph("Método de Aprovação:", bold_style), Paragraph(approval_method, normal_style)],
        [Paragraph("Status:", bold_style), Paragraph("Aprovado" if request.status in (RequestStatus.CONFIRMED, RequestStatus.ACCEPTED, RequestStatus.COMPLETED) else str(request.status.value), normal_style)],
        [Paragraph("Data de Aprovação:", bold_style), Paragraph(approval_date_str, normal_style)],
    ]
    if acceptance or otp_code != "N/A":
        data.append([Paragraph("Código OTP Utilizado:", bold_style), Paragraph(otp_code, normal_style)])
        
    data.extend([
        [Paragraph("Aprovado por:", bold_style), Paragraph(signer.full_name if signer else "N/A", normal_style)],
        [Paragraph("Observações:", bold_style), Paragraph(conf.observations if (conf and conf.observations) else "", normal_style)],
    ])

    col_widths = [160, 335]  # Total width 495
    t = Table(data, colWidths=col_widths)

    t_style = TableStyle([
        # Headers styling
        ('SPAN', (0, 0), (1, 0)),
        ('BACKGROUND', (0, 0), (1, 0), HexColor("#f8fafc")),
        ('BOTTOMPADDING', (0, 0), (1, 0), 6),
        ('TOPPADDING', (0, 0), (1, 0), 6),
        
        ('SPAN', (0, 3), (1, 3)),
        ('BACKGROUND', (0, 3), (1, 3), HexColor("#f8fafc")),
        ('BOTTOMPADDING', (0, 3), (1, 3), 6),
        ('TOPPADDING', (0, 3), (1, 3), 6),
        
        ('SPAN', (0, 13), (1, 13)),
        ('BACKGROUND', (0, 13), (1, 13), HexColor("#f8fafc")),
        ('BOTTOMPADDING', (0, 13), (1, 13), 6),
        ('TOPPADDING', (0, 13), (1, 13), 6),
        
        # General layout
        ('GRID', (0, 0), (-1, -1), 0.5, HexColor("#cbd5e1")),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        
        # Padding
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
    ])
    t.setStyle(t_style)

    # Wrap table to calculate height
    width, height = t.wrap(495, 600)
    
    # Draw table so that its top is at y = 700
    table_y = 700 - height
    t.drawOn(pdf, 50, table_y)

    # --- Footer ---
    pdf.setFont("Helvetica-Oblique", 8)
    pdf.setFillColor(HexColor("#475569"))
    footer_text = "Este documento é um comprovante oficial de agendamento. Qualquer alteração deve ser comunicada com antecedência mínima de 24 horas."
    pdf.drawCentredString(297.6, 60, footer_text)

    pdf.save()
    data_bytes = buffer.getvalue()
    file_path.write_bytes(data_bytes)

    digest = hashlib.sha256(data_bytes).hexdigest()
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
    ensure_user_scope(session, user, request)
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

    # Set all other pre-selected drivers to AVAILABLE
    conf = session.exec(
        select(OperationalConfirmation).where(OperationalConfirmation.request_id == request.id)
    ).first()
    if conf and conf.driver_ids:
        driver_ids_list = [int(d.strip()) for d in conf.driver_ids.split(",") if d.strip().isdigit()]
        for d_id in driver_ids_list:
            if d_id != dispatch.driver_id:
                other_driver = session.get(Driver, d_id)
                if other_driver:
                    other_driver.activity_status = DriverActivityStatus.AVAILABLE
                    other_driver.status_updated_at = now_utc()
                    session.add(other_driver)

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
    stmt = select(TravelRequest).where(
        TravelRequest.status.in_([RequestStatus.CONFIRMED, RequestStatus.ACCEPTED, RequestStatus.IN_EXECUTION, RequestStatus.COMPLETED])
    )
    if company_id:
        stmt = stmt.where(TravelRequest.company_id == company_id)
    if base_id:
        stmt = stmt.where(TravelRequest.base_id == base_id)

    return session.exec(stmt).all()


def billing_csv(session: Session, company_id: Optional[int], base_id: Optional[int]) -> str:
    rows = list_billable_requests(session, company_id=company_id, base_id=base_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["protocol", "company_id", "base_id", "status", "requested_datetime"])
    for row in rows:
        writer.writerow([row.protocol, row.company_id, row.base_id, row.status.value, row.requested_datetime.isoformat()])
    return output.getvalue()


def seed_data(session: Session) -> None:
    seed_runtime_data(session)
