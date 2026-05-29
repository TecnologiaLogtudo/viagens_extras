import re
import json
import asyncio
from datetime import timezone
from urllib.parse import quote_plus
from typing import List

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select, and_, delete

from app.auth import (
    SESSION_KEY,
    authenticate_user,
    can_access_finance,
    can_access_operations,
    company_only,
    hash_password,
    finance_or_manager,
    get_optional_user,
    partner_only,
    supervisor_or_manager,
)
from app.db import engine, get_session
from app.models import (
    Base,
    Company,
    CompanyBase,
    DecisionType,
    Driver,
    DriverActivityStatus,
    EventLog,
    OTPChallenge,
    OperationalConfirmation,
    RequestStatus,
    TriageDecisionPayload,
    TravelRequest,
    TravelRequestCreate,
    User,
    UserRole,
    UserBaseLink,
    UserCompanyBaseLink,
    Vehicle,
)
from app.services.datetime_utils import parse_form_datetime
from app.services.workflow import (
    can_partner_cancel_request,
    cancel_request,
    DomainError,
    ensure_aware,
    billing_csv,
    complete_trip,
    compute_sla,
    create_request,
    dispatch_trip,
    list_billable_requests,
    now_utc,
    propose_change,
    request_otp,
    resend_otp,
    sign_acceptance,
    supervisor_allowed_base_ids,
    supervisor_can_access_request,
    triage_request,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
PHONE_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")
PHONE_BR_DIGITS_RE = re.compile(r"^\d{10,11}$")


def _redirect(path: str):
    return RedirectResponse(url=path, status_code=303)


def _format_phone_br(raw_phone: str) -> str:
    digits = re.sub(r"\D", "", raw_phone)
    if len(digits) == 11:
        return f"{digits[:2]} {digits[2:7]}-{digits[7:]}"
    return f"{digits[:2]} {digits[2:6]}-{digits[6:]}"


def _role_home(user: User) -> str:
    if user.role == UserRole.PARTNER_REQUESTER:
        return "/partner"
    if user.role == UserRole.FINANCE_READONLY:
        return "/empresa/financeiro"
    if user.role == UserRole.LOGISTICS_MANAGER:
        return "/empresa/gerencial"
    return "/empresa/operacoes"


def _require_auth_or_redirect(request: Request, session: Session) -> User | RedirectResponse:
    user = get_optional_user(request, session)
    if not user:
        return _redirect("/login")
    return user


def _require_roles_or_redirect(
    request: Request,
    session: Session,
    allowed: tuple[UserRole, ...],
) -> User | RedirectResponse:
    user = _require_auth_or_redirect(request, session)
    if isinstance(user, RedirectResponse):
        return user
    if user.role not in allowed:
        raise HTTPException(status_code=403, detail="Sem permissão")
    return user


def _scoped_requests(session: Session, user: User):
    requests = session.exec(select(TravelRequest).order_by(TravelRequest.created_at.desc())).all()
    scoped = []

    for req in requests:
        if user.role == UserRole.PARTNER_REQUESTER and req.company_id != user.company_id:
            continue
        if user.role == UserRole.BASE_SUPERVISOR and not supervisor_can_access_request(session, user, req):
            continue
        scoped.append(req)
    return scoped


def _base_context(session: Session, user: User) -> dict:
    scoped = _scoped_requests(session, user)
    bases = session.exec(select(Base).order_by(Base.name)).all()
    sla_info = {}
    for req in scoped:
        base = next((b for b in bases if b.id == req.base_id), None)
        if base:
            sla_info[req.id] = compute_sla(req, base, session=session)

    counts = {
        "open": len([r for r in scoped if r.status in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE)]),
        "pending_acceptance": len([r for r in scoped if r.status == RequestStatus.CONFIRMED]),
        "in_execution": len([r for r in scoped if r.status == RequestStatus.IN_EXECUTION]),
        "completed": len([r for r in scoped if r.status == RequestStatus.COMPLETED]),
        "canceled": len([r for r in scoped if r.status == RequestStatus.CANCELED]),
    }

    common_labels = {
        RequestStatus.SUBMITTED: "Novo pedido",
        RequestStatus.TRIAGE: "Em triagem",
        RequestStatus.CONFIRMED: "Confirmado (aguardando aceite)",
        RequestStatus.ACCEPTED: "Aceito (aguardando despacho)",
        RequestStatus.IN_EXECUTION: "Em execução (viagem)",
        RequestStatus.COMPLETED: "Concluído",
        RequestStatus.REFUSED: "Recusado",
        RequestStatus.CANCELED: "Cancelado",
    }
    partner_labels = {
        RequestStatus.SUBMITTED: "Enviado",
        RequestStatus.CONFIRMED: "Aprovação pendente de aceite",
        RequestStatus.ACCEPTED: "Aceito",
        RequestStatus.IN_EXECUTION: "Em execução",
    }
    supervisor_labels = {
        RequestStatus.SUBMITTED: "Pendente triagem",
        RequestStatus.TRIAGE: "Em triagem",
        RequestStatus.CONFIRMED: "Confirmado (Pendente aceite)",
        RequestStatus.ACCEPTED: "Aceito (Pendente despacho)",
        RequestStatus.IN_EXECUTION: "Em execução (despachado)",
    }

    labels: dict[int, str] = {}
    cancel_available: dict[int, bool] = {}
    confirmations: dict[int, OperationalConfirmation] = {}
    for req in scoped:
        if user.role == UserRole.PARTNER_REQUESTER:
            label = partner_labels.get(req.status) or common_labels.get(req.status) or req.status.value
            cancel_available[req.id] = can_partner_cancel_request(session, req)
        elif user.role in (UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER):
            label = supervisor_labels.get(req.status) or common_labels.get(req.status) or req.status.value
            cancel_available[req.id] = False
        else:
            label = common_labels.get(req.status) or req.status.value
            cancel_available[req.id] = False
        labels[req.id] = label
        
        # Fetch confirmation for all (partners need to see it too)
        conf = session.exec(
            select(OperationalConfirmation).where(OperationalConfirmation.request_id == req.id)
        ).first()
        if conf:
            confirmations[req.id] = conf

    return {
        "travel_requests": scoped,
        "bases": {b.id: b for b in bases},
        "sla_info": sla_info,
        "counts": counts,
        "status_labels": labels,
        "cancel_available": cancel_available,
        "confirmations": confirmations,
        "roles": UserRole,
        "status": RequestStatus,
        "driver_status": DriverActivityStatus,
        "can_access_finance": can_access_finance(user),
        "can_access_operations": can_access_operations(user),
    }


def _manager_context(session: Session) -> dict:
    bases = session.exec(select(Base).where(Base.active == True).order_by(Base.name)).all()
    companies = session.exec(select(Company).where(Company.active == True).order_by(Company.name)).all()
    supervisors = session.exec(select(User).where(User.role == UserRole.BASE_SUPERVISOR)).all()
    partners = session.exec(select(User).where(User.role == UserRole.PARTNER_REQUESTER)).all()
    company_base_links = session.exec(select(CompanyBase).order_by(CompanyBase.company_id, CompanyBase.base_id)).all()
    base_by_id = {base.id: base for base in bases}
    base_company_ids_by_base_id: dict[int, list[int]] = {base.id: [] for base in bases}
    company_bases_by_company_id: dict[int, list[Base]] = {company.id: [] for company in companies}
    company_base_meta_by_company_id: dict[int, list[dict]] = {company.id: [] for company in companies}
    company_base_choices_by_company_id: dict[int, list[dict]] = {company.id: [] for company in companies}
    for link in company_base_links:
        base = base_by_id.get(link.base_id)
        if not base:
            continue
        base_company_ids_by_base_id.setdefault(base.id, []).append(link.company_id)
        company_bases_by_company_id.setdefault(link.company_id, []).append(base)
        company_base_meta_by_company_id.setdefault(link.company_id, []).append(
            {"id": link.id, "base": base, "contract_sla_minutes": link.contract_sla_minutes}
        )
        company_base_choices_by_company_id.setdefault(link.company_id, []).append(
            {"id": link.id, "base": base, "contract_sla_minutes": link.contract_sla_minutes}
        )
    for base_list in company_bases_by_company_id.values():
        base_list.sort(key=lambda base: base.name.lower())
    for meta_list in company_base_meta_by_company_id.values():
        meta_list.sort(key=lambda item: item["base"].name.lower())
    for choice_list in company_base_choices_by_company_id.values():
        choice_list.sort(key=lambda item: (item["base"].name.lower(), (item["base"].location or "").lower()))

    supervisor_company_base_link_ids = {}
    supervisor_company_names = {}
    supervisor_base_labels = {}
    for sup in supervisors:
        selected_links = session.exec(
            select(CompanyBase)
            .join(UserCompanyBaseLink, UserCompanyBaseLink.company_base_id == CompanyBase.id)
            .where(UserCompanyBaseLink.user_id == sup.id)
            .order_by(CompanyBase.company_id, CompanyBase.base_id)
        ).all()
        supervisor_company_base_link_ids[sup.id] = [link.id for link in selected_links]
        names = []
        labels = []
        seen_company_ids = set()
        for link in selected_links:
            company = session.get(Company, link.company_id)
            base = session.get(Base, link.base_id)
            if company and company.id not in seen_company_ids:
                names.append(company.name)
                seen_company_ids.add(company.id)
            if base:
                labels.append(f"{company.name if company else 'Empresa'} - {base.name} - {base.location or 'Local não informado'}")
        supervisor_company_names[sup.id] = sorted(names, key=str.lower)
        supervisor_base_labels[sup.id] = labels

    partner_base_names = {}
    for p in partners:
        partner_base_names[p.id] = sorted((b.name for b in p.bases), key=str.lower)

    company_base_info = session.exec(select(CompanyBase).order_by(CompanyBase.company_id, CompanyBase.base_id)).all()
    return {
        "bases": bases,
        "companies": companies,
        "company_base_links": company_base_links,
        "base_company_ids_by_base_id": base_company_ids_by_base_id,
        "company_bases_by_company_id": company_bases_by_company_id,
        "company_base_meta_by_company_id": company_base_meta_by_company_id,
        "company_base_choices_by_company_id": company_base_choices_by_company_id,
        "supervisors": supervisors,
        "partners": partners,
        "supervisor_company_base_link_ids": supervisor_company_base_link_ids,
        "supervisor_company_names": supervisor_company_names,
        "supervisor_base_labels": supervisor_base_labels,
        "partner_base_names": partner_base_names,
        "company_base_info": company_base_info,
    }


def _request_visible_to_user(session: Session, request_id: int | None, user: User) -> bool:
    if request_id is None:
        return user.role == UserRole.LOGISTICS_MANAGER

    req = session.get(TravelRequest, request_id)
    if not req:
        return False
    if user.role == UserRole.LOGISTICS_MANAGER:
        return True
    if user.role == UserRole.PARTNER_REQUESTER:
        return req.company_id == user.company_id
    if user.role == UserRole.BASE_SUPERVISOR:
        return supervisor_can_access_request(session, user, req)
    if user.role == UserRole.FINANCE_READONLY:
        return req.status == RequestStatus.COMPLETED
    return False


def _event_matches_user(session: Session, event: EventLog, user: User) -> bool:
    request_event_types = {
        "request_changed",
        "request_submitted",
        "request_confirmed",
        "request_refused",
        "request_modified_by_partner",
        "request_canceled",
        "acceptance_signed",
        "trip_dispatched",
        "trip_completed",
        "otp_sent",
    }
    if event.event_type == "manager_data_changed":
        return user.role == UserRole.LOGISTICS_MANAGER
    if event.event_type == "driver_status_changed":
        if user.role not in (UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER):
            return False
        if user.role == UserRole.LOGISTICS_MANAGER:
            return True
        if not event.request_id:
            return False
        req = session.get(TravelRequest, event.request_id)
        return bool(req and supervisor_can_access_request(session, user, req))
    if event.event_type in request_event_types:
        return _request_visible_to_user(session, event.request_id, user)
    return False


def _sse_event_payload(event: EventLog) -> dict:
    if event.event_type == "driver_status_changed":
        return {"kind": "driver_status_changed", "request_id": event.request_id}
    if event.event_type == "manager_data_changed":
        return {"kind": "manager_data_changed"}
    return {"kind": "request_changed", "request_id": event.request_id}


def _resolve_company_from_base_ids(session: Session, base_ids: list[int]) -> Company | None:
    company_ids_by_base_id: dict[int, set[int]] = {}
    for base_id in base_ids:
        links = session.exec(select(CompanyBase).where(CompanyBase.base_id == base_id)).all()
        company_ids_by_base_id[base_id] = {link.company_id for link in links}

    common_company_ids: set[int] | None = None
    for company_ids in company_ids_by_base_id.values():
        if not company_ids:
            return None
        common_company_ids = company_ids if common_company_ids is None else common_company_ids & company_ids
        if not common_company_ids:
            return None

    if not common_company_ids or len(common_company_ids) != 1:
        return None

    company_id = next(iter(common_company_ids))
    return session.get(Company, company_id)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)):
    user = get_optional_user(request, session)
    if not user:
        return _redirect("/login")
    return _redirect(_role_home(user))


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: Session = Depends(get_session)):
    user = get_optional_user(request, session)
    if user:
        return _redirect(_role_home(user))

    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
            "message": request.query_params.get("message"),
            "title": "Entrar | Central de Viagens Extras",
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login_action(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    user = authenticate_user(session, email.strip().lower(), password)
    if not user:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Credenciais inválidas.",
                "message": None,
                "title": "Entrar | Central de Viagens Extras",
            },
            status_code=401,
        )
    request.session[SESSION_KEY] = user.id
    return _redirect(_role_home(user))


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return _redirect("/login")


@router.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return templates.TemplateResponse(
        "signup.html",
        {
            "request": request,
            "error": None,
            "title": "Cadastro | Central de Viagens Extras",
        },
    )


@router.post("/signup", response_class=HTMLResponse)
def signup_action(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    company_name: str = Form(...),
    phone: str = Form(default=""),
    job_title: str = Form(default=""),
    address: str = Form(default=""),
    session: Session = Depends(get_session),
):
    normalized_email = email.strip().lower()
    normalized_name = full_name.strip()
    normalized_company = company_name.strip()
    normalized_phone = phone.strip()

    error = None
    if not normalized_name:
        error = "Nome completo é obrigatório."
    elif not normalized_company:
        error = "Empresa é obrigatória."
    elif len(password) < 8:
        error = "A senha deve ter no mínimo 8 caracteres."
    elif normalized_phone and not PHONE_E164_RE.match(normalized_phone):
        error = "Telefone inválido. Use formato E.164, por exemplo +5571999990001."
    elif session.exec(select(User).where(User.email == normalized_email)).first():
        error = "Este e-mail já está cadastrado."

    if error:
        return templates.TemplateResponse(
            "signup.html",
            {
                "request": request,
                "error": error,
                "title": "Cadastro | Central de Viagens Extras",
            },
            status_code=400,
        )

    user = User(
        full_name=normalized_name,
        email=normalized_email,
        role=UserRole.PARTNER_REQUESTER,
        company_name=normalized_company,
        phone=normalized_phone or None,
        job_title=job_title.strip() or None,
        address=address.strip() or None,
        password_hash=hash_password(password),
        is_active=True,
    )
    session.add(user)
    session.commit()
    return _redirect("/login?message=Cadastro+realizado+com+sucesso.+Faca+login.")


@router.get("/partner", response_class=HTMLResponse)
def partner_portal(
    request: Request,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.PARTNER_REQUESTER,))
    if isinstance(user, RedirectResponse):
        return user
    ctx = _base_context(session, user)
    return templates.TemplateResponse(
        "partner_portal.html",
        {
            "request": request,
            "user": user,
            **ctx,
            "message": request.query_params.get("message"),
            "title": "Portal Parceiro",
        },
    )


@router.get("/partner/settings", response_class=HTMLResponse)
def partner_settings_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    return templates.TemplateResponse(
        "partner_settings.html",
        {
            "request": request,
            "user": user,
            "error": request.query_params.get("error"),
            "message": request.query_params.get("message"),
            "title": "Configurações | Portal Parceiro",
        },
    )


@router.post("/partner/settings")
def partner_settings_save(
    full_name: str = Form(...),
    email: str = Form(...),
    company_name: str = Form(...),
    phone: str = Form(default=""),
    job_title: str = Form(default=""),
    new_password: str = Form(default=""),
    confirm_password: str = Form(default=""),
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    normalized_name = full_name.strip()
    normalized_email = email.strip().lower()
    normalized_company = company_name.strip()
    phone_digits = re.sub(r"\D", "", phone.strip())

    if not normalized_name:
        return _redirect("/partner/settings?error=Nome+completo+e+obrigatorio.")
    if not normalized_company:
        return _redirect("/partner/settings?error=Empresa+e+obrigatoria.")
    existing_email = session.exec(select(User).where(User.email == normalized_email, User.id != user.id)).first()
    if existing_email:
        return _redirect("/partner/settings?error=Este+e-mail+ja+esta+em+uso.")
    if phone_digits and not PHONE_BR_DIGITS_RE.match(phone_digits):
        return _redirect("/partner/settings?error=Telefone+invalido.+Use+DDD+com+10+ou+11+digitos.")
    if new_password and len(new_password) < 8:
        return _redirect("/partner/settings?error=A+nova+senha+deve+ter+no+minimo+8+caracteres.")
    if new_password and new_password != confirm_password:
        return _redirect("/partner/settings?error=Confirmacao+de+senha+nao+confere.")

    user.full_name = normalized_name
    user.email = normalized_email
    user.company_name = normalized_company
    user.phone = _format_phone_br(phone_digits) if phone_digits else None
    user.job_title = job_title.strip() or None
    if new_password:
        user.password_hash = hash_password(new_password)
    session.add(user)
    session.commit()
    return _redirect("/partner/settings?message=Dados+atualizados+com+sucesso.")


@router.get("/empresa", response_class=HTMLResponse)
def company_home(
    request: Request,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(
        request,
        session,
        (UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER, UserRole.FINANCE_READONLY),
    )
    if isinstance(user, RedirectResponse):
        return user
    return _redirect(_role_home(user))


@router.get("/empresa/operacoes", response_class=HTMLResponse)
def company_operations(
    request: Request,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER))
    if isinstance(user, RedirectResponse):
        return user
    ctx = _base_context(session, user)
    selected_request = None
    drivers_for_selected = []
    vehicles_for_selected = []
    selected_request_id = request.query_params.get("selected_request_id")
    if selected_request_id and selected_request_id.isdigit():
        req = session.get(TravelRequest, int(selected_request_id))
        if req:
            if user.role != UserRole.BASE_SUPERVISOR or supervisor_can_access_request(session, user, req):
                selected_request = req
                drivers_for_selected = session.exec(
                    select(Driver).where(Driver.base_id == req.base_id).order_by(Driver.name)
                ).all()
                vehicles_for_selected = session.exec(
                    select(Vehicle).where(Vehicle.base_id == req.base_id).order_by(Vehicle.plate)
                ).all()
    return templates.TemplateResponse(
        "company_operations.html",
        {
            "request": request,
            "user": user,
            **ctx,
            "selected_request": selected_request,
            "drivers_for_selected": drivers_for_selected,
            "vehicles_for_selected": vehicles_for_selected,
            "title": "Empresa | Operações",
        },
    )


@router.get("/empresa/gerencial", response_class=HTMLResponse)
def company_manager(
    request: Request,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.LOGISTICS_MANAGER,))
    if isinstance(user, RedirectResponse):
        return user
    manager_ctx = _manager_context(session)
    edit_supervisor = None
    edit_partner = None
    edit_supervisor_company_base_link_ids = []
    edit_partner_base_ids = []

    edit_supervisor_id = request.query_params.get("edit_supervisor_id")
    if edit_supervisor_id and edit_supervisor_id.isdigit():
        candidate = session.get(User, int(edit_supervisor_id))
        if candidate and candidate.role == UserRole.BASE_SUPERVISOR:
            edit_supervisor = candidate
            edit_supervisor_company_base_link_ids = [
                link.id
                for link in session.exec(
                    select(CompanyBase)
                    .join(UserCompanyBaseLink, UserCompanyBaseLink.company_base_id == CompanyBase.id)
                    .where(UserCompanyBaseLink.user_id == candidate.id)
                    .order_by(CompanyBase.company_id, CompanyBase.base_id)
                ).all()
            ]

    edit_partner_id = request.query_params.get("edit_partner_id")
    if edit_partner_id and edit_partner_id.isdigit():
        candidate = session.get(User, int(edit_partner_id))
        if candidate and candidate.role == UserRole.PARTNER_REQUESTER:
            edit_partner = candidate
            edit_partner_base_ids = [b.id for b in edit_partner.bases]

    edit_base_id = request.query_params.get("edit_base_id")
    edit_base = None
    edit_base_company_ids = []
    if edit_base_id and edit_base_id.isdigit():
        candidate = session.get(Base, int(edit_base_id))
        if candidate:
            edit_base = candidate
            edit_base_company_ids = manager_ctx.get("base_company_ids_by_base_id", {}).get(edit_base.id, [])

    edit_company_id = request.query_params.get("edit_company_id")
    edit_company = None
    if edit_company_id and edit_company_id.isdigit():
        candidate = session.get(Company, int(edit_company_id))
        if candidate:
            edit_company = candidate

    return templates.TemplateResponse(
        "company_manager.html",
        {
            "request": request,
            "user": user,
            **manager_ctx,
            "roles": UserRole,
            "title": "Empresa | Gerencial",
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
            "edit_supervisor": edit_supervisor,
            "edit_partner": edit_partner,
            "edit_supervisor_company_base_link_ids": edit_supervisor_company_base_link_ids,
            "edit_partner_base_ids": edit_partner_base_ids,
            "edit_base": edit_base,
            "edit_base_company_ids": edit_base_company_ids,
            "edit_company": edit_company,
        },
    )


@router.post("/empresa/gerencial/supervisors/new")
def register_supervisor(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    phone: str = Form(default=""),
    password: str = Form(...),
    company_base_link_ids: List[int] = Form(...),
    supervisor_id: int | None = Form(None),
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.LOGISTICS_MANAGER,))
    if isinstance(user, RedirectResponse):
        return user
        
    if len(company_base_link_ids) == 0:
        return _redirect("/empresa/gerencial?error=Selecione+ao+menos+uma+base+para+o+supervisor")

    normalized_email = email.strip().lower()
    phone_digits = re.sub(r"\D", "", phone.strip())
    normalized_phone = _format_phone_br(phone_digits) if phone_digits else None
    existing = session.exec(select(User).where(User.email == normalized_email)).first()
    if existing and (supervisor_id is None or existing.id != supervisor_id):
        return _redirect("/empresa/gerencial?error=Email+já+cadastrado")

    unique_company_base_link_ids = sorted(set(company_base_link_ids))
    if supervisor_id:
        supervisor = session.get(User, supervisor_id)
        if not supervisor or supervisor.role != UserRole.BASE_SUPERVISOR:
            return _redirect("/empresa/gerencial?error=Supervisor+invalido")
        supervisor.full_name = full_name
        supervisor.email = normalized_email
        supervisor.phone = normalized_phone
        if password:
            supervisor.password_hash = hash_password(password)
        session.exec(delete(UserBaseLink).where(UserBaseLink.user_id == supervisor.id))
        session.exec(delete(UserCompanyBaseLink).where(UserCompanyBaseLink.user_id == supervisor.id))
        valid_links = session.exec(
            select(CompanyBase).where(CompanyBase.id.in_(unique_company_base_link_ids))
        ).all()
        if len(valid_links) != len(unique_company_base_link_ids):
            return _redirect("/empresa/gerencial?error=Base+invalida+selecionada")
        for link in valid_links:
            session.add(UserCompanyBaseLink(user_id=supervisor.id, company_base_id=link.id))
        session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="supervisor_updated"))
        session.commit()
        return _redirect("/empresa/gerencial?message=Supervisor+atualizado+com+sucesso")

    new_user = User(
        full_name=full_name,
        email=normalized_email,
        phone=normalized_phone,
        role=UserRole.BASE_SUPERVISOR,
        password_hash=hash_password(password),
        is_active=True
    )
    session.add(new_user)
    session.flush()
    valid_links = session.exec(
        select(CompanyBase).where(CompanyBase.id.in_(unique_company_base_link_ids))
    ).all()
    if len(valid_links) != len(unique_company_base_link_ids):
        return _redirect("/empresa/gerencial?error=Base+invalida+selecionada")
    for link in valid_links:
        session.add(UserCompanyBaseLink(user_id=new_user.id, company_base_id=link.id))
        
    session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="supervisor_created"))
    session.commit()
    return _redirect("/empresa/gerencial?message=Supervisor+cadastrado+com+sucesso")


@router.post("/empresa/gerencial/partners/new")
def register_partner(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    phone: str = Form(default=""),
    base_ids: List[int] = Form(...),
    partner_id: int | None = Form(None),
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.LOGISTICS_MANAGER,))
    if isinstance(user, RedirectResponse):
        return user
        
    if len(base_ids) == 0:
        return _redirect("/empresa/gerencial?error=Selecione+ao+menos+uma+base+para+o+parceiro")
    normalized_email = email.strip().lower()
    existing = session.exec(select(User).where(User.email == normalized_email)).first()
    if existing and (partner_id is None or existing.id != partner_id):
        return _redirect("/empresa/gerencial?error=Email+já+cadastrado")
    phone_digits = re.sub(r"\D", "", phone.strip())
    if phone_digits and not PHONE_BR_DIGITS_RE.match(phone_digits):
        return _redirect("/empresa/gerencial?error=Telefone+invalido.+Use+DDD+com+10+ou+11+digitos.")

    unique_base_ids = sorted(set(base_ids))
    company = _resolve_company_from_base_ids(session, unique_base_ids)
    if not company:
        return _redirect("/empresa/gerencial?error=Não+foi+possível+identificar+a+empresa+a+partir+das+bases+selecionadas")

    if partner_id:
        partner = session.get(User, partner_id)
        if not partner or partner.role != UserRole.PARTNER_REQUESTER:
            return _redirect("/empresa/gerencial?error=Parceiro+invalido")
        partner.full_name = full_name
        partner.email = normalized_email
        partner.phone = _format_phone_br(phone_digits) if phone_digits else None
        if password:
            partner.password_hash = hash_password(password)
        partner.company_id = company.id
        partner.company_name = company.name
        session.exec(delete(UserBaseLink).where(UserBaseLink.user_id == partner.id))
        for b_id in unique_base_ids:
            base = session.get(Base, b_id)
            if not base:
                return _redirect("/empresa/gerencial?error=Base+invalida+selecionada")
            session.add(UserBaseLink(user_id=partner.id, base_id=b_id))
        session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="partner_updated"))
        session.commit()
        return _redirect("/empresa/gerencial?message=Parceiro+atualizado+com+sucesso")

    new_user = User(
        full_name=full_name,
        email=normalized_email,
        role=UserRole.PARTNER_REQUESTER,
        company_id=company.id,
        company_name=company.name,
        phone=_format_phone_br(phone_digits) if phone_digits else None,
        password_hash=hash_password(password),
        is_active=True
    )
    session.add(new_user)
    session.flush()
    
    for b_id in unique_base_ids:
        base = session.get(Base, b_id)
        if not base:
            return _redirect("/empresa/gerencial?error=Base+invalida+selecionada")
        session.add(UserBaseLink(user_id=new_user.id, base_id=b_id))
        
    session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="partner_created_or_updated"))
    session.commit()
    return _redirect("/empresa/gerencial?message=Parceiro+cadastrado+com+sucesso.+Vinculos+de+base+atualizados.")


@router.post("/empresa/gerencial/supervisors/{supervisor_id}/delete")
def delete_supervisor(
    request: Request,
    supervisor_id: int,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.LOGISTICS_MANAGER,))
    if isinstance(user, RedirectResponse):
        return user
    supervisor = session.get(User, supervisor_id)
    if not supervisor or supervisor.role != UserRole.BASE_SUPERVISOR:
        return _redirect("/empresa/gerencial?error=Supervisor+invalido")
    session.exec(delete(UserBaseLink).where(UserBaseLink.user_id == supervisor.id))
    session.exec(delete(UserCompanyBaseLink).where(UserCompanyBaseLink.user_id == supervisor.id))
    session.delete(supervisor)
    session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="supervisor_deleted"))
    session.commit()
    return _redirect("/empresa/gerencial?message=Supervisor+deletado+com+sucesso")


@router.post("/empresa/gerencial/partners/{partner_id}/delete")
def delete_partner(
    request: Request,
    partner_id: int,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.LOGISTICS_MANAGER,))
    if isinstance(user, RedirectResponse):
        return user
    partner = session.get(User, partner_id)
    if not partner or partner.role != UserRole.PARTNER_REQUESTER:
        return _redirect("/empresa/gerencial?error=Parceiro+invalido")
    session.exec(delete(UserBaseLink).where(UserBaseLink.user_id == partner.id))
    session.delete(partner)
    session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="partner_deleted"))
    session.commit()
    return _redirect("/empresa/gerencial?message=Parceiro+deletado+com+sucesso")


@router.get("/empresa/financeiro", response_class=HTMLResponse)
def company_finance(
    request: Request,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.FINANCE_READONLY, UserRole.LOGISTICS_MANAGER))
    if isinstance(user, RedirectResponse):
        return user
    items = list_billable_requests(session, None, None)
    item_status_labels = {item.id: ("Concluído" if item.status == RequestStatus.COMPLETED else item.status.value) for item in items}
    return templates.TemplateResponse(
        "company_finance.html",
        {
            "request": request,
            "user": user,
            "items": items,
            "item_status_labels": item_status_labels,
            "title": "Empresa | Financeiro",
        },
    )


@router.get("/partner/fragments/metrics", response_class=HTMLResponse)
def partner_metrics_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    ctx = _base_context(session, user)
    return templates.TemplateResponse(
        "_partner_metrics.html",
        {"request": request, "user": user, **ctx},
    )


@router.get("/partner/fragments/requests", response_class=HTMLResponse)
def partner_requests_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    ctx = _base_context(session, user)
    return templates.TemplateResponse(
        "_partner_requests.html",
        {"request": request, "user": user, **ctx},
    )


@router.get("/empresa/operacoes/fragments/metrics", response_class=HTMLResponse)
def operations_metrics_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    ctx = _base_context(session, user)
    return templates.TemplateResponse(
        "_operations_metrics.html",
        {"request": request, "user": user, **ctx},
    )


@router.get("/empresa/operacoes/fragments/requests", response_class=HTMLResponse)
def operations_requests_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    ctx = _base_context(session, user)
    return templates.TemplateResponse(
        "_operations_requests.html",
        {"request": request, "user": user, **ctx},
    )


@router.get("/empresa/operacoes/fragments/supervisor-panel", response_class=HTMLResponse)
def operations_supervisor_panel_fragment(
    request: Request,
    selected_request_id: int | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    if not selected_request_id:
        return templates.TemplateResponse(
            "_supervisor_panel.html",
            {
                "request": request,
                "user": user,
                "selected_request": None,
                "selected_request_status_label": None,
                "drivers_for_selected": [],
                "vehicles_for_selected": [],
                "driver_status": DriverActivityStatus,
            },
        )
    req = session.get(TravelRequest, selected_request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    if user.role == UserRole.BASE_SUPERVISOR and not supervisor_can_access_request(session, user, req):
        raise HTTPException(status_code=403, detail="Sem permissão para esta solicitação")
    drivers = session.exec(select(Driver).where(Driver.base_id == req.base_id).order_by(Driver.name)).all()
    vehicles = session.exec(select(Vehicle).where(Vehicle.base_id == req.base_id).order_by(Vehicle.plate)).all()
    status_label = {
        RequestStatus.TRIAGE: "Triagem",
        RequestStatus.CONFIRMED: "Confirmado",
        RequestStatus.ACCEPTED: "Aceito pelo parceiro",
        RequestStatus.IN_EXECUTION: "Em execução (despachado)",
        RequestStatus.COMPLETED: "Concluído",
        RequestStatus.REFUSED: "Recusado",
        RequestStatus.CANCELED: "Cancelado",
    }.get(req.status, req.status.value)
    return templates.TemplateResponse(
        "_supervisor_panel.html",
        {
            "request": request,
            "user": user,
            "selected_request": req,
            "selected_request_status_label": status_label,
            "drivers_for_selected": drivers,
            "vehicles_for_selected": vehicles,
            "driver_status": DriverActivityStatus,
        },
    )


@router.get("/empresa/gerencial/fragments/supervisors", response_class=HTMLResponse)
def manager_supervisors_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(finance_or_manager),
):
    if user.role != UserRole.LOGISTICS_MANAGER:
        raise HTTPException(status_code=403, detail="Sem permissão")
    ctx = _manager_context(session)
    return templates.TemplateResponse(
        "_manager_supervisors.html",
        {"request": request, "user": user, **ctx},
    )


@router.get("/empresa/gerencial/fragments/partners", response_class=HTMLResponse)
def manager_partners_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(finance_or_manager),
):
    if user.role != UserRole.LOGISTICS_MANAGER:
        raise HTTPException(status_code=403, detail="Sem permissão")
    ctx = _manager_context(session)
    return templates.TemplateResponse(
        "_manager_partners.html",
        {"request": request, "user": user, **ctx},
    )


@router.get("/empresa/gerencial/fragments/contracts", response_class=HTMLResponse)
def manager_contracts_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(finance_or_manager),
):
    if user.role != UserRole.LOGISTICS_MANAGER:
        raise HTTPException(status_code=403, detail="Sem permissão")
    ctx = _manager_context(session)
    return templates.TemplateResponse(
        "_manager_contracts.html",
        {"request": request, "user": user, **ctx},
    )


@router.get("/empresa/gerencial/fragments/bases", response_class=HTMLResponse)
def manager_bases_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(finance_or_manager),
):
    if user.role != UserRole.LOGISTICS_MANAGER:
        raise HTTPException(status_code=403, detail="Sem permissão")
    ctx = _manager_context(session)
    return templates.TemplateResponse("_manager_bases.html", {"request": request, "user": user, **ctx})


@router.get("/empresa/gerencial/fragments/companies", response_class=HTMLResponse)
def manager_companies_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(finance_or_manager),
):
    if user.role != UserRole.LOGISTICS_MANAGER:
        raise HTTPException(status_code=403, detail="Sem permissão")
    ctx = _manager_context(session)
    return templates.TemplateResponse("_manager_companies.html", {"request": request, "user": user, **ctx})


@router.post("/empresa/gerencial/bases/new")
def register_base(
    request: Request,
    name: str = Form(...),
    location: str = Form(default=""),
    sla_minutes: int = Form(30),
    company_ids: List[int] | None = Form(default=None),
    base_id: int | None = Form(None),
    session: Session = Depends(get_session),
    user: User = Depends(finance_or_manager),
):
    if user.role != UserRole.LOGISTICS_MANAGER:
        raise HTTPException(status_code=403, detail="Sem permissão")
    normalized_name = name.strip()
    if not normalized_name:
        return _redirect("/empresa/gerencial?error=Nome+da+base+é+obrigatório")

    selected_company_ids = sorted(set(company_ids or []))
    selected_companies_by_id = {}
    if selected_company_ids:
        selected_companies = session.exec(
            select(Company).where(and_(Company.active == True, Company.id.in_(selected_company_ids)))
        ).all()
        selected_companies_by_id = {company.id: company for company in selected_companies}
        if len(selected_companies_by_id) != len(selected_company_ids):
            return _redirect("/empresa/gerencial?error=Empresa+invalida")

    if base_id:
        base = session.get(Base, base_id)
        if not base:
            return _redirect("/empresa/gerencial?error=Base+invalida")
        base.name = normalized_name
        base.location = location.strip() or None
        base.sla_minutes = sla_minutes
        existing_links = session.exec(select(CompanyBase).where(CompanyBase.base_id == base.id)).all()
        for link in existing_links:
            if link.company_id not in selected_company_ids:
                session.delete(link)
        for company_id, company in selected_companies_by_id.items():
            existing_link = session.exec(
                select(CompanyBase).where(and_(CompanyBase.company_id == company.id, CompanyBase.base_id == base.id))
            ).first()
            if not existing_link:
                session.add(CompanyBase(company_id=company.id, base_id=base.id))
        session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="base_updated"))
        session.commit()
        return _redirect("/empresa/gerencial?message=Base+atualizada+com+sucesso")

    new_base = Base(name=normalized_name, location=location.strip() or None, sla_minutes=sla_minutes)
    session.add(new_base)
    session.flush()
    for company_id, company in selected_companies_by_id.items():
        session.add(CompanyBase(company_id=company.id, base_id=new_base.id))
    session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="base_created"))
    session.commit()
    return _redirect("/empresa/gerencial?message=Base+cadastrada+com+sucesso")


@router.post("/empresa/gerencial/bases/{base_id}/delete")
def delete_base(
    request: Request,
    base_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(finance_or_manager),
):
    if user.role != UserRole.LOGISTICS_MANAGER:
        raise HTTPException(status_code=403, detail="Sem permissão")
    base = session.get(Base, base_id)
    if not base:
        return _redirect("/empresa/gerencial?error=Base+invalida")
    # Prevent deletion if companies are linked
    linked = session.exec(select(CompanyBase).where(CompanyBase.base_id == base.id)).first()
    if linked:
        return _redirect("/empresa/gerencial?error=Não+é+possível+excluir+uma+base+com+empresas+vinculadas")
    session.delete(base)
    session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="base_deleted"))
    session.commit()
    return _redirect("/empresa/gerencial?message=Base+deletada+com+sucesso")


@router.post("/empresa/gerencial/companies/new")
def register_company(
    request: Request,
    company_name: str = Form(...),
    base_ids: List[int] = Form(...),
    sla_minutes: int = Form(30),
    company_id: int | None = Form(None),
    session: Session = Depends(get_session),
    user: User = Depends(finance_or_manager),
):
    if user.role != UserRole.LOGISTICS_MANAGER:
        raise HTTPException(status_code=403, detail="Sem permissão")
    normalized_name = company_name.strip()
    if not normalized_name:
        return _redirect("/empresa/gerencial?error=Nome+da+empresa+é+obrigatório")

    company = session.exec(select(Company).where(Company.name == normalized_name)).first()
    company_created = False
    if not company:
        company = Company(name=normalized_name, cnpj="PENDENTE")
        session.add(company)
        session.flush()
        company_created = True

    unique_base_ids = sorted(set(base_ids))
    if company_id:
        c = session.get(Company, company_id)
        if not c:
            return _redirect("/empresa/gerencial?error=Empresa+invalida")
        c.name = normalized_name
        # adjust links: remove links not selected, update or create selected
        existing_links = session.exec(select(CompanyBase).where(CompanyBase.company_id == c.id)).all()
        existing_base_ids = {l.base_id for l in existing_links}
        # remove unselected
        for l in existing_links:
            if l.base_id not in unique_base_ids:
                session.delete(l)
        # add/update selected
        for b_id in unique_base_ids:
            base = session.get(Base, b_id)
            if not base:
                return _redirect("/empresa/gerencial?error=Base+invalida+selecionada")
            existing_cb = session.exec(select(CompanyBase).where(and_(CompanyBase.company_id == c.id, CompanyBase.base_id == b_id))).first()
            if existing_cb:
                existing_cb.contract_sla_minutes = sla_minutes
                session.add(existing_cb)
            else:
                session.add(CompanyBase(company_id=c.id, base_id=b_id, contract_sla_minutes=sla_minutes))
        session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="company_updated"))
        session.commit()
        if company_created:
            return _redirect("/empresa/gerencial?message=Empresa+atualizada+com+sucesso.+Empresa+criada+automaticamente.")
        return _redirect("/empresa/gerencial?message=Empresa+atualizada+com+sucesso")

    # create new company with links
    session.add(company)
    session.flush()
    for b_id in unique_base_ids:
        base = session.get(Base, b_id)
        if not base:
            return _redirect("/empresa/gerencial?error=Base+invalida+selecionada")
        session.add(CompanyBase(company_id=company.id, base_id=b_id, contract_sla_minutes=sla_minutes))
    session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="company_created"))
    session.commit()
    return _redirect("/empresa/gerencial?message=Empresa+cadastrada+com+sucesso")


@router.post("/empresa/gerencial/companies/{company_id}/delete")
def delete_company(
    request: Request,
    company_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(finance_or_manager),
):
    if user.role != UserRole.LOGISTICS_MANAGER:
        raise HTTPException(status_code=403, detail="Sem permissão")
    company = session.get(Company, company_id)
    if not company:
        return _redirect("/empresa/gerencial?error=Empresa+invalida")
    # prevent deletion if partners exist
    linked_user = session.exec(select(User).where(User.company_id == company.id)).first()
    if linked_user:
        return _redirect("/empresa/gerencial?error=Não+é+possível+excluir+empresa+com+parceiros+vinculados")
    session.exec(delete(CompanyBase).where(CompanyBase.company_id == company.id))
    session.delete(company)
    session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="company_deleted"))
    session.commit()
    return _redirect("/empresa/gerencial?message=Empresa+deletada+com+sucesso")


@router.get("/events/stream")
async def events_stream(
    request: Request,
    session: Session = Depends(get_session),
):
    user = get_optional_user(request, session)
    if not user:
        raise HTTPException(status_code=401, detail="Não autenticado")

    last_id_header = request.headers.get("last-event-id")
    last_event_id = int(last_id_header) if last_id_header and last_id_header.isdigit() else 0

    async def event_generator():
        nonlocal last_event_id
        while True:
            if await request.is_disconnected():
                break

            with Session(engine) as stream_session:
                events = stream_session.exec(
                    select(EventLog).where(EventLog.id > last_event_id).order_by(EventLog.id.asc())
                ).all()
                for event in events:
                    if _event_matches_user(stream_session, event, user):
                        payload = _sse_event_payload(event)
                        yield f"id: {event.id}\nevent: {event.event_type}\ndata: {json.dumps(payload)}\n\n"
                    last_event_id = max(last_event_id, event.id or 0)

            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/empresa/requests/{request_id}/supervisor-panel", response_class=HTMLResponse)
def supervisor_panel(
    request_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")
    
    # Supervisor multiple bases check
    if user.role == UserRole.BASE_SUPERVISOR and not supervisor_can_access_request(session, user, req):
        raise HTTPException(status_code=403, detail="Sem permissão para esta solicitação")

    drivers = session.exec(select(Driver).where(Driver.base_id == req.base_id).order_by(Driver.name)).all()
    vehicles = session.exec(select(Vehicle).where(Vehicle.base_id == req.base_id).order_by(Vehicle.plate)).all()
    status_label = {
        RequestStatus.TRIAGE: "Triagem",
        RequestStatus.CONFIRMED: "Confirmado",
        RequestStatus.ACCEPTED: "Aceito pelo parceiro",
        RequestStatus.IN_EXECUTION: "Em execução (despachado)",
        RequestStatus.COMPLETED: "Concluído",
        RequestStatus.REFUSED: "Recusado",
        RequestStatus.CANCELED: "Cancelado",
    }.get(req.status, req.status.value)
    return templates.TemplateResponse(
        "_supervisor_panel.html",
        {
            "request": request,
            "user": user,
            "selected_request": req,
            "selected_request_status_label": status_label,
            "drivers_for_selected": drivers,
            "vehicles_for_selected": vehicles,
            "driver_status": DriverActivityStatus,
        },
    )


@router.post("/partner/requests/new")
def new_request(
    base_id: int = Form(...),
    request_type: str = Form(...),
    requested_datetime: str = Form(...),
    origin: str = Form(...),
    destination: str = Form(...),
    quantity: int = Form(...),
    vehicle_type_requested: str = Form(...),
    cost_center: str = Form(default=""),
    reason: str = Form(default=""),
    notes: str = Form(default=""),
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    try:
        payload = TravelRequestCreate(
            base_id=base_id,
            request_type=request_type,
            requested_datetime=parse_form_datetime(requested_datetime),
            origin=origin,
            destination=destination,
            quantity=quantity,
            vehicle_type_requested=vehicle_type_requested,
            cost_center=cost_center,
            reason=reason,
            notes=notes or None,
        )
        create_request(session, user, payload)
    except DomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _redirect("/partner")


@router.post("/partner/requests/{request_id}/otp")
def otp_send(
    request_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")

    # Unify: if a valid (not expired, not consumed) OTP exists, just go to verify page
    challenge = session.exec(
        select(OTPChallenge)
        .where(
            OTPChallenge.request_id == request_id,
            OTPChallenge.user_id == user.id,
            OTPChallenge.consumed_at == None,
        )
        .order_by(OTPChallenge.created_at.desc())
    ).first()

    if challenge and ensure_aware(challenge.expires_at) > now_utc():
        return _redirect(f"/partner/requests/{request_id}/otp-verify")

    try:
        request_otp(session, user, req)
    except DomainError as exc:
        return _redirect(f"/partner?message={quote_plus(str(exc))}")
    return _redirect(f"/partner/requests/{request_id}/otp-verify")


@router.get("/partner/requests/{request_id}/otp-verify", response_class=HTMLResponse)
def otp_verify_page(
    request_id: int,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")
    if req.company_id != user.company_id:
        raise HTTPException(403, "Sem permissão")

    challenge = session.exec(
        select(OTPChallenge)
        .where(OTPChallenge.request_id == request_id, OTPChallenge.user_id == user.id)
        .order_by(OTPChallenge.created_at.desc())
    ).first()

    remaining_seconds = 0
    resend_available_at_epoch = int(now_utc().timestamp())
    resend_count = 0
    if challenge:
        expires_at = challenge.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        remaining_seconds = max(0, int((expires_at - now_utc()).total_seconds()))
        resend_count = challenge.resend_count
        if challenge.last_resend_at:
            last_resend_at = challenge.last_resend_at
            if last_resend_at.tzinfo is None:
                last_resend_at = last_resend_at.replace(tzinfo=timezone.utc)
            resend_available_at_epoch = int(last_resend_at.timestamp()) + 60

    return templates.TemplateResponse(
        "otp_verification.html",
        {
            "request": request,
            "user": user,
            "travel_request": req,
            "remaining_seconds": remaining_seconds,
            "resend_count": resend_count,
            "resend_limit": 3,
            "resend_available_at_epoch": resend_available_at_epoch,
            "error": request.query_params.get("error"),
            "message": request.query_params.get("message"),
            "title": f"OTP {req.protocol}",
        },
    )


@router.post("/partner/requests/{request_id}/accept")
def accept(
    request_id: int,
    otp_code: str = Form(...),
    request: Request = None,
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")

    try:
        sign_acceptance(
            session,
            user,
            req,
            otp_code,
            ip=(request.client.host if request and request.client else "0.0.0.0"),
            user_agent=(request.headers.get("user-agent", "unknown") if request else "unknown"),
        )
    except DomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _redirect("/partner?message=Aceite+confirmado+com+sucesso.")


@router.post("/partner/requests/{request_id}/cancel")
def cancel_partner_request(
    request_id: int,
    reason: str = Form(default=""),
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")
    try:
        cancel_request(session, user, req, reason=reason or None)
    except DomainError as exc:
        return _redirect(f"/partner?message={quote_plus(str(exc))}")
    return _redirect("/partner?message=Pedido+cancelado+com+sucesso.")


@router.post("/partner/requests/{request_id}/propose-change")
def partner_propose_change(
    request_id: int,
    requested_datetime: str = Form(...),
    origin: str = Form(...),
    destination: str = Form(...),
    quantity: int = Form(...),
    vehicle_type_requested: str = Form(...),
    cost_center: str = Form(default=""),
    reason: str = Form(default=""),
    notes: str = Form(default=""),
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")
    try:
        payload = TravelRequestCreate(
            base_id=req.base_id,
            request_type=req.request_type,
            requested_datetime=parse_form_datetime(requested_datetime),
            origin=origin,
            destination=destination,
            quantity=quantity,
            vehicle_type_requested=vehicle_type_requested,
            cost_center=cost_center,
            reason=reason,
            notes=notes or None,
        )
        propose_change(session, user, req, payload)
    except DomainError as exc:
        return _redirect(f"/partner?message={quote_plus(str(exc))}")

    return _redirect("/partner?message=Alteração+proposta+com+sucesso.+Aguarde+nova+triagem.")


@router.post("/partner/requests/{request_id}/otp/resend")
def otp_resend(
    request_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")
    try:
        resend_otp(session, user, req)
    except DomainError as exc:
        return _redirect(f"/partner/requests/{request_id}/otp-verify?error={quote_plus(str(exc))}")
    return _redirect(
        f"/partner/requests/{request_id}/otp-verify?message=Novo+codigo+enviado+para+seu+email."
    )


@router.post("/empresa/requests/{request_id}/triage")
def triage(
    request_id: int,
    decision_type: DecisionType = Form(...),
    approved_quantity: int = Form(default=1),
    confirmed_datetime: str = Form(default="2030-01-01T10:00:00+00:00"),
    confirmed_vehicle_type: str = Form(default="sedan"),
    tariff_value: float = Form(default=0.0),
    observations: str = Form(default=""),
    refusal_reason: str = Form(default=""),
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")

    try:
        payload = TriageDecisionPayload(
            decision_type=decision_type,
            approved_quantity=approved_quantity,
            confirmed_datetime=parse_form_datetime(confirmed_datetime),
            confirmed_vehicle_type=confirmed_vehicle_type,
            tariff_value=tariff_value,
            observations=observations or None,
            refusal_reason=refusal_reason or None,
        )
        triage_request(session, user, req, payload)
    except DomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _redirect("/empresa/operacoes")


@router.post("/empresa/drivers/{driver_id}/status")
def change_driver_status(
    request: Request,
    driver_id: int,
    status: DriverActivityStatus = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    request_id = None
    selected_request_id = request.query_params.get("selected_request_id")
    if selected_request_id and selected_request_id.isdigit():
        req = session.get(TravelRequest, int(selected_request_id))
        if req:
            if user.role == UserRole.BASE_SUPERVISOR and not supervisor_can_access_request(session, user, req):
                raise HTTPException(status_code=403, detail="Sem permissão para esta solicitação")
            request_id = req.id

    driver = session.get(Driver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Motorista não encontrado")
    
    # Supervisor multiple bases check
    allowed_base_ids = supervisor_allowed_base_ids(session, user) if user.role == UserRole.BASE_SUPERVISOR else set()
    if user.role == UserRole.BASE_SUPERVISOR and driver.base_id not in allowed_base_ids:
        raise HTTPException(status_code=403, detail="Sem permissão para este motorista")

    driver.activity_status = status
    driver.status_updated_at = now_utc()
    session.add(driver)
    session.add(
        EventLog(
            request_id=request_id,
            actor_user_id=user.id,
            event_type="driver_status_changed",
            payload=f"driver_id={driver.id}|status={status.value}",
        )
    )
    session.commit()
    redirect_url = "/empresa/operacoes"
    if request_id:
        redirect_url = f"/empresa/operacoes?selected_request_id={request_id}"
    return _redirect(redirect_url)


@router.post("/empresa/requests/{request_id}/dispatch")
def dispatch(
    request_id: int,
    driver_id: int = Form(...),
    vehicle_id: int = Form(...),
    planned_departure_at: str = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")

    try:
        dispatch_trip(session, user, req, driver_id, vehicle_id, parse_form_datetime(planned_departure_at))
    except DomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _redirect("/empresa/operacoes")


@router.post("/empresa/requests/{request_id}/complete")
def complete(
    request_id: int,
    actual_departure_at: str = Form(...),
    actual_arrival_at: str = Form(...),
    occurrences: str = Form(default=""),
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")

    try:
        complete_trip(
            session,
            user,
            req,
            parse_form_datetime(actual_departure_at),
            parse_form_datetime(actual_arrival_at),
            occurrences or None,
        )
    except DomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return _redirect("/empresa/operacoes")


@router.get("/empresa/financeiro/billing.csv")
def finance_csv(
    company_id: int | None = None,
    base_id: int | None = None,
    session: Session = Depends(get_session),
    user: User = Depends(finance_or_manager),
):
    csv_data = billing_csv(session, company_id=company_id, base_id=base_id)
    return Response(
        content=csv_data,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=faturamento.csv"},
    )
