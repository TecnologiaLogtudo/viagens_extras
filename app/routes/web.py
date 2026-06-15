import re
import json
import asyncio
from datetime import timezone
from pathlib import Path
from urllib.parse import quote_plus
from typing import List

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select, and_, delete, func

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
    login_required,
)
from app.db import engine, get_session
from app.models import (
    Base,
    Company,
    CompanyBase,
    DecisionType,
    Driver,
    DriverActivityStatus,
    Document,
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
    generate_pdf_document,
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


def _form_int_list(form_data, key: str) -> list[int]:
    raw_values = form_data.getlist(key)
    parsed_values: list[int] = []
    for raw_value in raw_values:
        text = str(raw_value).strip()
        if not text:
            continue
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        for part in re.split(r"[,\s]+", text):
            part = part.strip()
            if part:
                parsed_values.append(int(part))
    if parsed_values:
        return parsed_values

    raw_value = form_data.get(key)
    if raw_value is None:
        return []
    text = str(raw_value).strip()
    if not text:
        return []
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    return [int(part.strip()) for part in re.split(r"[,\s]+", text) if part.strip()]


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
        "open": len([r for r in scoped if r.status in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE, RequestStatus.ACCEPTED)]),
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

    raw_types = session.exec(select(Vehicle.vehicle_type)).all()
    vehicle_types = sorted(list({vt.upper() for vt in raw_types if vt}))
    if not vehicle_types:
        vehicle_types = ["SEDAN", "SUV", "VAN", "KOMBI", "MOTO"]

    all_drivers = session.exec(select(Driver)).all()
    drivers_map = {d.id: d for d in all_drivers}

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
        "can_manage_drivers": False,
        "driver_rows": _driver_rows(session, supervisor_allowed_base_ids(session, user) if user.role == UserRole.BASE_SUPERVISOR else None)[0]
        if user.role in (UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER)
        else [],
        "can_access_finance": can_access_finance(user),
        "can_access_operations": can_access_operations(user),
        "vehicle_types": vehicle_types,
        "drivers_map": drivers_map,
    }


def _driver_rows(
    session: Session,
    base_ids: set[int] | list[int] | None = None,
    filter_base_id: int | None = None,
    filter_vehicle_type: str | None = None,
    page: int | None = None,
    per_page: int = 12,
) -> tuple[list[dict], int]:
    query = select(Driver, Base, Vehicle).join(Base, Driver.base_id == Base.id).outerjoin(Vehicle, Driver.vehicle_id == Vehicle.id)
    
    # 1. Enforce supervisor allowed base restrictions or apply manager filters
    if base_ids is not None:
        base_id_list = sorted(set(base_ids))
        if not base_id_list:
            return [], 0
        if filter_base_id is not None:
            if filter_base_id in base_id_list:
                query = query.where(Driver.base_id == filter_base_id)
            else:
                return [], 0
        else:
            query = query.where(Driver.base_id.in_(base_id_list))
    else:
        if filter_base_id is not None:
            query = query.where(Driver.base_id == filter_base_id)

    # 2. Apply vehicle category filter case-insensitively
    if filter_vehicle_type:
        query = query.where(func.lower(Vehicle.vehicle_type) == filter_vehicle_type.strip().lower())

    all_results = session.exec(query.order_by(Driver.name, Base.name, Vehicle.plate)).all()
    total_count = len(all_results)

    # Slicing results in Python for robustness
    if page is not None:
        start = (page - 1) * per_page
        end = start + per_page
        results = all_results[start:end]
    else:
        results = all_results

    rows = []
    for driver, base, vehicle in results:
        rows.append(
            {
                "id": driver.id,
                "name": driver.name,
                "phone": driver.phone,
                "base_id": driver.base_id,
                "active": driver.active,
                "activity_status": driver.activity_status,
                "base_name": base.name,
                "base_location": base.location,
                "vehicle_type": vehicle.vehicle_type if vehicle else None,
                "plate": vehicle.plate if vehicle else None,
            }
        )
    return rows, total_count


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
        "driver_rows": _driver_rows(session)[0],
        "can_manage_drivers": True,
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
        return user.role in (UserRole.LOGISTICS_MANAGER, UserRole.BASE_SUPERVISOR)
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


def _resolve_company_from_company_base_link_ids(
    session: Session, company_base_link_ids: list[int]
) -> tuple[Company | None, list[CompanyBase]]:
    unique_company_base_link_ids = sorted(set(company_base_link_ids))
    if not unique_company_base_link_ids:
        return None, []

    company_base_links = session.exec(
        select(CompanyBase)
        .where(CompanyBase.id.in_(unique_company_base_link_ids))
        .order_by(CompanyBase.company_id, CompanyBase.base_id)
    ).all()
    if len(company_base_links) != len(unique_company_base_link_ids):
        return None, []

    company_ids = {link.company_id for link in company_base_links}
    if len(company_ids) != 1:
        return None, []

    company = session.get(Company, next(iter(company_ids)))
    if not company:
        return None, []

    return company, company_base_links


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


@router.get("/empresa/motoristas", response_class=HTMLResponse)
def company_drivers(
    request: Request,
    filter_base_id: str | None = None,
    filter_vehicle_type: str | None = None,
    page: int = 1,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER))
    if isinstance(user, RedirectResponse):
        return user
    can_manage_drivers = user.role in (UserRole.LOGISTICS_MANAGER, UserRole.BASE_SUPERVISOR)
    allowed_base_ids = supervisor_allowed_base_ids(session, user) if user.role == UserRole.BASE_SUPERVISOR else None
    
    actual_base_id = int(filter_base_id) if filter_base_id and filter_base_id.strip().isdigit() else None
    driver_rows, total_count = _driver_rows(
        session,
        allowed_base_ids,
        filter_base_id=actual_base_id,
        filter_vehicle_type=filter_vehicle_type,
        page=page,
    )
    total_pages = (total_count + 11) // 12
    
    # Query bases available for filtering (restrict to supervisor's bases if base_supervisor)
    if allowed_base_ids is not None:
        filter_bases = session.exec(
            select(Base).where(Base.id.in_(sorted(set(allowed_base_ids)))).order_by(Base.name, Base.location)
        ).all()
    else:
        filter_bases = session.exec(
            select(Base).where(Base.active == True).order_by(Base.name, Base.location)
        ).all()

    if can_manage_drivers:
        if allowed_base_ids is not None:
            bases = session.exec(
                select(Base).where(and_(Base.active == True, Base.id.in_(sorted(set(allowed_base_ids))))).order_by(Base.name, Base.location)
            ).all()
        else:
            bases = session.exec(
                select(Base).where(Base.active == True).order_by(Base.name, Base.location)
            ).all()
    else:
        bases = []
    edit_driver = None
    error = request.query_params.get("error")
    if can_manage_drivers:
        edit_driver_id = request.query_params.get("edit_driver_id")
        if edit_driver_id and edit_driver_id.isdigit():
            driver_obj = session.get(Driver, int(edit_driver_id))
            if driver_obj:
                if user.role == UserRole.BASE_SUPERVISOR and allowed_base_ids is not None:
                    if driver_obj.base_id in allowed_base_ids:
                        edit_driver = driver_obj
                    else:
                        error = "Você não tem permissão para editar este motorista"
                else:
                    edit_driver = driver_obj
    message = request.query_params.get("message")
    return templates.TemplateResponse(
        "company_drivers.html",
        {
            "request": request,
            "user": user,
            "driver_rows": driver_rows,
            "bases": bases,
            "filter_bases": filter_bases,
            "filter_base_id": actual_base_id,
            "filter_vehicle_type": filter_vehicle_type,
            "page": page,
            "total_pages": total_pages,
            "edit_driver": edit_driver,
            "can_manage_drivers": can_manage_drivers,
            "message": message,
            "error": error,
            "title": "Empresa | Motoristas",
        },
    )


@router.post("/empresa/motoristas/save")
def save_driver(
    request: Request,
    driver_id: int | None = Form(None),
    name: str = Form(...),
    phone: str = Form(default=""),
    base_id: int = Form(...),
    vehicle_type: str | None = Form(default=None),
    plate: str | None = Form(default=None),
    active: str | None = Form(default=None),
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.LOGISTICS_MANAGER, UserRole.BASE_SUPERVISOR))
    if isinstance(user, RedirectResponse):
        return user

    normalized_name = name.strip()
    phone_digits = re.sub(r"\D", "", phone.strip())
    if not normalized_name:
        return _redirect(f"/empresa/motoristas?error={quote_plus('Nome é obrigatório')}")
    if phone_digits and not PHONE_BR_DIGITS_RE.match(phone_digits):
        return _redirect(
            f"/empresa/motoristas?error={quote_plus('Telefone inválido. Use DDD com 10 ou 11 dígitos.')}"
        )

    base = session.get(Base, base_id)
    if not base or not base.active:
        return _redirect(f"/empresa/motoristas?error={quote_plus('Base inválida')}")

    if user.role == UserRole.BASE_SUPERVISOR:
        allowed = supervisor_allowed_base_ids(session, user)
        if base_id not in allowed:
            return _redirect(f"/empresa/motoristas?error={quote_plus('Você não tem permissão para esta base')}")

    # Handle vehicle linking/creation/updating
    normalized_plate = plate.strip().upper() if plate else ""
    normalized_vehicle_type = vehicle_type.strip().upper() if vehicle_type else ""

    vehicle_id_to_link = None
    if normalized_plate:
        if not normalized_vehicle_type:
            return _redirect(
                f"/empresa/motoristas?error={quote_plus('Categoria do veículo é obrigatória quando a placa é fornecida')}"
            )
        
        # Check if another vehicle has this plate
        existing_vehicle = session.exec(
            select(Vehicle).where(Vehicle.plate == normalized_plate)
        ).first()
        
        if existing_vehicle:
            # Update the existing vehicle details to match
            existing_vehicle.vehicle_type = normalized_vehicle_type
            existing_vehicle.base_id = base.id
            session.add(existing_vehicle)
            session.flush()
            vehicle_id_to_link = existing_vehicle.id
        else:
            # Create a new vehicle
            new_vehicle = Vehicle(
                plate=normalized_plate,
                vehicle_type=normalized_vehicle_type,
                base_id=base.id,
                active=True,
            )
            session.add(new_vehicle)
            session.flush()
            vehicle_id_to_link = new_vehicle.id

    target_driver = None
    if driver_id is not None:
        target_driver = session.get(Driver, driver_id)
        if not target_driver:
            return _redirect(f"/empresa/motoristas?error={quote_plus('Motorista inválido')}")
        if user.role == UserRole.BASE_SUPERVISOR:
            allowed = supervisor_allowed_base_ids(session, user)
            if target_driver.base_id not in allowed:
                return _redirect(f"/empresa/motoristas?error={quote_plus('Você não tem permissão para editar este motorista')}")
    else:
        target_driver = session.exec(
            select(Driver).where(and_(Driver.name == normalized_name, Driver.base_id == base.id))
        ).first()

    phone_value = _format_phone_br(phone_digits) if phone_digits else ""
    # When editing an existing driver, honor the active checkbox if present.
    # When creating a new driver (no checkbox in the form) default to active=True.
    if target_driver:
        is_active = active is not None
        target_driver.name = normalized_name
        target_driver.phone = phone_value
        target_driver.base_id = base.id
        target_driver.active = is_active
        target_driver.vehicle_id = vehicle_id_to_link
        session.add(target_driver)
        session.add(
            EventLog(
                actor_user_id=user.id,
                event_type="manager_data_changed",
                payload=f"driver_updated|driver_id={target_driver.id}",
            )
        )
        session.commit()
        return _redirect(f"/empresa/motoristas?message={quote_plus('Motorista atualizado com sucesso')}")

    new_driver = Driver(
        name=normalized_name,
        phone=phone_value,
        base_id=base.id,
        active=True,
        vehicle_id=vehicle_id_to_link,
    )
    session.add(new_driver)
    session.flush()
    session.add(
        EventLog(
            actor_user_id=user.id,
            event_type="manager_data_changed",
            payload=f"driver_created|driver_id={new_driver.id}",
        )
    )
    session.commit()
    return _redirect(f"/empresa/motoristas?message={quote_plus('Motorista cadastrado com sucesso')}")


@router.post("/empresa/motoristas/bulk-delete")
async def bulk_delete_drivers(
    request: Request,
    driver_ids: list[str] | None = Form(None),
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.LOGISTICS_MANAGER, UserRole.BASE_SUPERVISOR))
    if isinstance(user, RedirectResponse):
        return user

    # Fallback to request.form() if the Form dependency didn't capture the list
    if not driver_ids:
        try:
            form = await request.form()
            driver_ids = form.getlist("driver_ids")
        except Exception:
            pass

    driver_ids_raw = [str(i) for i in (driver_ids or [])]
    unique_driver_ids = sorted({int(did) for did in driver_ids_raw if str(did).isdigit()})
    
    if not unique_driver_ids:
        return _redirect(f"/empresa/motoristas?error={quote_plus('Selecione ao menos um motorista')}")

    allowed = None
    if user.role == UserRole.BASE_SUPERVISOR:
        allowed = supervisor_allowed_base_ids(session, user)

    deleted_count = 0
    for driver_id in unique_driver_ids:
        driver = session.get(Driver, driver_id)
        if not driver:
            continue
        if allowed is not None and driver.base_id not in allowed:
            continue
        session.delete(driver)
        deleted_count += 1

    session.add(
        EventLog(
            actor_user_id=user.id,
            event_type="manager_data_changed",
            payload=f"driver_bulk_deleted|count={deleted_count}",
        )
    )
    session.commit()
    return _redirect(
        f"/empresa/motoristas?message={quote_plus(f'{deleted_count} motorista(s) excluído(s) com sucesso')}"
    )


@router.post("/empresa/motoristas/{driver_id}/delete")
def delete_driver(
    request: Request,
    driver_id: int,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.LOGISTICS_MANAGER, UserRole.BASE_SUPERVISOR))
    if isinstance(user, RedirectResponse):
        return user

    driver = session.get(Driver, driver_id)
    if not driver:
        return _redirect(f"/empresa/motoristas?error={quote_plus('Motorista não encontrado')}")

    if user.role == UserRole.BASE_SUPERVISOR:
        allowed = supervisor_allowed_base_ids(session, user)
        if driver.base_id not in allowed:
            return _redirect(f"/empresa/motoristas?error={quote_plus('Você não tem permissão para excluir este motorista')}")

    session.delete(driver)
    session.add(
        EventLog(
            actor_user_id=user.id,
            event_type="manager_data_changed",
            payload=f"driver_deleted|driver_id={driver_id}",
        )
    )
    session.commit()
    return _redirect(f"/empresa/motoristas?message={quote_plus('Motorista excluído com sucesso')}")


@router.post("/empresa/motoristas/{driver_id}/toggle-active")
def toggle_driver_active(
    request: Request,
    driver_id: int,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.LOGISTICS_MANAGER, UserRole.BASE_SUPERVISOR))
    if isinstance(user, RedirectResponse):
        return user

    driver = session.get(Driver, driver_id)
    if not driver:
        return _redirect(f"/empresa/motoristas?error={quote_plus('Motorista não encontrado')}")

    if user.role == UserRole.BASE_SUPERVISOR:
        allowed = supervisor_allowed_base_ids(session, user)
        if driver.base_id not in allowed:
            return _redirect(f"/empresa/motoristas?error={quote_plus('Você não tem permissão para alterar o status deste motorista')}")

    driver.active = not driver.active
    session.add(driver)
    session.add(
        EventLog(
            actor_user_id=user.id,
            event_type="manager_data_changed",
            payload=f"driver_active_toggled|driver_id={driver.id}|active={driver.active}",
        )
    )
    session.commit()
    
    state_str = "ativado" if driver.active else "desativado"
    return _redirect(f"/empresa/motoristas?message={quote_plus(f'Motorista {state_str} com sucesso')}")


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
    edit_partner_company_base_link_ids = []

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
            partner_base_ids = [b.id for b in edit_partner.bases if b.id is not None]
            if edit_partner.company_id is not None and partner_base_ids:
                edit_partner_company_base_link_ids = [
                    link.id
                    for link in session.exec(
                        select(CompanyBase)
                        .where(
                            CompanyBase.company_id == edit_partner.company_id,
                            CompanyBase.base_id.in_(sorted(set(partner_base_ids))),
                        )
                        .order_by(CompanyBase.base_id)
                    ).all()
                ]

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
            "edit_partner_company_base_link_ids": edit_partner_company_base_link_ids,
            "edit_base": edit_base,
            "edit_base_company_ids": edit_base_company_ids,
            "edit_company": edit_company,
        },
    )


@router.post("/empresa/gerencial/supervisors/new")
async def register_supervisor(
    request: Request,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.LOGISTICS_MANAGER,))
    if isinstance(user, RedirectResponse):
        return user

    form = await request.form()
    full_name = str(form.get("full_name", "")).strip()
    email = str(form.get("email", "")).strip()
    phone = str(form.get("phone", "")).strip()
    password = str(form.get("password", ""))
    supervisor_id_raw = form.get("supervisor_id")
    supervisor_id = int(supervisor_id_raw) if str(supervisor_id_raw).strip().isdigit() else None
    company_base_link_ids = _form_int_list(form, "company_base_link_ids")
    if not company_base_link_ids:
        base_ids = _form_int_list(form, "base_ids")
        if base_ids:
            company_base_link_ids = [
                link.id
                for link in session.exec(select(CompanyBase).where(CompanyBase.base_id.in_(sorted(set(base_ids))))).all()
                if link.id is not None
            ]

    if len(company_base_link_ids) == 0:
        return _redirect(f"/empresa/gerencial?error={quote_plus('Selecione ao menos uma base para o supervisor')}")

    normalized_email = email.lower()
    phone_digits = re.sub(r"\D", "", phone)
    normalized_phone = _format_phone_br(phone_digits) if phone_digits else None
    existing = session.exec(select(User).where(User.email == normalized_email)).first()
    if existing and (supervisor_id is None or existing.id != supervisor_id):
        return _redirect(f"/empresa/gerencial?error={quote_plus('Email já cadastrado')}")

    unique_company_base_link_ids = sorted(set(company_base_link_ids))
    if supervisor_id:
        supervisor = session.get(User, supervisor_id)
        if not supervisor or supervisor.role != UserRole.BASE_SUPERVISOR:
            return _redirect(f"/empresa/gerencial?error={quote_plus('Supervisor inválido')}")
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
            return _redirect(f"/empresa/gerencial?error={quote_plus('Base inválida selecionada')}")
        for link in valid_links:
            session.add(UserCompanyBaseLink(user_id=supervisor.id, company_base_id=link.id))
        session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="supervisor_updated"))
        session.commit()
        return _redirect(f"/empresa/gerencial?message={quote_plus('Supervisor atualizado com sucesso')}")

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
        return _redirect(f"/empresa/gerencial?error={quote_plus('Base inválida selecionada')}")
    for link in valid_links:
        session.add(UserCompanyBaseLink(user_id=new_user.id, company_base_id=link.id))
        
    session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="supervisor_created"))
    session.commit()
    return _redirect(f"/empresa/gerencial?message={quote_plus('Supervisor cadastrado com sucesso')}")


@router.post("/empresa/gerencial/partners/new")
async def register_partner(
    request: Request,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.LOGISTICS_MANAGER,))
    if isinstance(user, RedirectResponse):
        return user

    form = await request.form()
    full_name = str(form.get("full_name", "")).strip()
    email = str(form.get("email", "")).strip()
    password = str(form.get("password", ""))
    phone = str(form.get("phone", "")).strip()
    partner_id_raw = form.get("partner_id")
    partner_id = int(partner_id_raw) if str(partner_id_raw).strip().isdigit() else None
    min_advance_minutes_raw = form.get("min_advance_minutes")
    min_advance_minutes = int(min_advance_minutes_raw) if str(min_advance_minutes_raw).strip().isdigit() else 0
    company_base_link_ids = _form_int_list(form, "company_base_link_ids")
    legacy_base_ids = _form_int_list(form, "base_ids")

    selected_base_ids: list[int] = []
    company: Company | None = None

    if company_base_link_ids:
        company, company_base_links = _resolve_company_from_company_base_link_ids(session, company_base_link_ids)
        if not company:
            return _redirect(
                f"/empresa/gerencial?error={quote_plus('Não foi possível identificar a empresa pelos vínculos selecionados. Escolha vínculos da mesma empresa.')}"
            )
        selected_base_ids = sorted({link.base_id for link in company_base_links})
    elif legacy_base_ids:
        unique_base_ids = sorted(set(legacy_base_ids))
        company = _resolve_company_from_base_ids(session, unique_base_ids)
        if not company:
            return _redirect(
                f"/empresa/gerencial?error={quote_plus('Não foi possível identificar a empresa pelas bases selecionadas. Escolha bases da mesma empresa.')}"
            )
        selected_base_ids = unique_base_ids
    else:
        return _redirect(f"/empresa/gerencial?error={quote_plus('Selecione ao menos uma base para o parceiro')}")

    normalized_email = email.lower()
    existing = session.exec(select(User).where(User.email == normalized_email)).first()
    if existing and (partner_id is None or existing.id != partner_id):
        return _redirect(f"/empresa/gerencial?error={quote_plus('Email já cadastrado')}")
    phone_digits = re.sub(r"\D", "", phone.strip())
    if phone_digits and not PHONE_BR_DIGITS_RE.match(phone_digits):
        return _redirect(
            f"/empresa/gerencial?error={quote_plus('Telefone inválido. Use DDD com 10 ou 11 dígitos.')}"
        )

    if partner_id:
        partner = session.get(User, partner_id)
        if not partner or partner.role != UserRole.PARTNER_REQUESTER:
            return _redirect(f"/empresa/gerencial?error={quote_plus('Parceiro inválido')}")
        partner.full_name = full_name
        partner.email = normalized_email
        partner.phone = _format_phone_br(phone_digits) if phone_digits else None
        if password:
            partner.password_hash = hash_password(password)
        partner.company_id = company.id
        partner.company_name = company.name
        partner.min_advance_minutes = min_advance_minutes
        session.exec(delete(UserBaseLink).where(UserBaseLink.user_id == partner.id))
        for b_id in selected_base_ids:
            base = session.get(Base, b_id)
            if not base:
                return _redirect(f"/empresa/gerencial?error={quote_plus('Base inválida selecionada')}")
            existing_company_base = session.exec(
                select(CompanyBase).where(
                    CompanyBase.company_id == company.id,
                    CompanyBase.base_id == base.id,
                )
            ).first()
            if not existing_company_base:
                if company_base_link_ids:
                    return _redirect(
                        f"/empresa/gerencial?error={quote_plus('Vínculo de base inválido selecionado')}"
                    )
                session.add(
                    CompanyBase(
                        company_id=company.id,
                        base_id=base.id,
                        contract_sla_minutes=base.sla_minutes,
                    )
                )
            session.add(UserBaseLink(user_id=partner.id, base_id=b_id))
        session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="partner_updated"))
        session.commit()
        return _redirect(f"/empresa/gerencial?message={quote_plus('Parceiro atualizado com sucesso')}")

    new_user = User(
        full_name=full_name,
        email=normalized_email,
        role=UserRole.PARTNER_REQUESTER,
        company_id=company.id,
        company_name=company.name,
        phone=_format_phone_br(phone_digits) if phone_digits else None,
        password_hash=hash_password(password),
        min_advance_minutes=min_advance_minutes,
        is_active=True
    )
    session.add(new_user)
    session.flush()
    
    for b_id in selected_base_ids:
        base = session.get(Base, b_id)
        if not base:
            return _redirect(f"/empresa/gerencial?error={quote_plus('Base inválida selecionada')}")
        existing_company_base = session.exec(
            select(CompanyBase).where(
                CompanyBase.company_id == company.id,
                CompanyBase.base_id == base.id,
                )
            ).first()
        if not existing_company_base:
            if company_base_link_ids:
                return _redirect(f"/empresa/gerencial?error={quote_plus('Vínculo de base inválido selecionado')}")
            session.add(
                CompanyBase(
                    company_id=company.id,
                    base_id=base.id,
                    contract_sla_minutes=base.sla_minutes,
                )
            )
        session.add(UserBaseLink(user_id=new_user.id, base_id=b_id))
        
    session.add(EventLog(actor_user_id=user.id, event_type="manager_data_changed", payload="partner_created_or_updated"))
    session.commit()
    return _redirect(
        f"/empresa/gerencial?message={quote_plus('Parceiro cadastrado com sucesso. Vínculos de base atualizados.')}"
    )


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


@router.get("/empresa/operacoes/fragments/drivers", response_class=HTMLResponse)
def operations_drivers_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    ctx = _base_context(session, user)
    return templates.TemplateResponse(
        "_drivers_table.html",
        {"request": request, "user": user, "table_id": "operations-drivers", "driver_rows": ctx.get("driver_rows", [])},
    )


@router.get("/empresa/motoristas/fragments/list", response_class=HTMLResponse)
def drivers_list_fragment(
    request: Request,
    filter_base_id: str | None = None,
    filter_vehicle_type: str | None = None,
    page: int = 1,
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    allowed_base_ids = supervisor_allowed_base_ids(session, user) if user.role == UserRole.BASE_SUPERVISOR else None
    actual_base_id = int(filter_base_id) if filter_base_id and filter_base_id.strip().isdigit() else None
    driver_rows, total_count = _driver_rows(
        session,
        allowed_base_ids,
        filter_base_id=actual_base_id,
        filter_vehicle_type=filter_vehicle_type,
        page=page,
    )
    total_pages = (total_count + 11) // 12
    return templates.TemplateResponse(
        "_drivers_table.html",
        {
            "request": request,
            "user": user,
            "table_id": "drivers-list",
            "driver_rows": driver_rows,
            "page": page,
            "total_pages": total_pages,
            "filter_base_id": actual_base_id,
            "filter_vehicle_type": filter_vehicle_type,
        },
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
    confirmation = session.exec(
        select(OperationalConfirmation).where(OperationalConfirmation.request_id == req.id)
    ).first()
    drivers = session.exec(select(Driver).where(Driver.base_id == req.base_id).order_by(Driver.name)).all()
    vehicles = session.exec(select(Vehicle).where(Vehicle.base_id == req.base_id).order_by(Vehicle.plate)).all()

    # Filter drivers by requested or confirmed vehicle type
    allowed_types = set()
    if confirmation and confirmation.confirmed_vehicle_type:
        allowed_types = {t.strip().lower() for t in confirmation.confirmed_vehicle_type.split(",") if t.strip()}
    elif req.vehicle_type_requested:
        allowed_types = {t.strip().lower() for t in req.vehicle_type_requested.split(",") if t.strip()}

    if allowed_types:
        drivers = [
            d for d in drivers
            if not d.vehicle or d.vehicle.vehicle_type.lower() in allowed_types
        ]

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
            "confirmation": confirmation,
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


@router.get("/empresa/gerencial/fragments/drivers", response_class=HTMLResponse)
def manager_drivers_fragment(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(finance_or_manager),
):
    if user.role != UserRole.LOGISTICS_MANAGER:
        raise HTTPException(status_code=403, detail="Sem permissão")
    ctx = _manager_context(session)
    return templates.TemplateResponse(
        "_drivers_table.html",
        {"request": request, "user": user, "table_id": "manager-drivers", "driver_rows": ctx.get("driver_rows", [])},
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

    confirmation = session.exec(
        select(OperationalConfirmation).where(OperationalConfirmation.request_id == req.id)
    ).first()
    drivers = session.exec(select(Driver).where(Driver.base_id == req.base_id).order_by(Driver.name)).all()
    vehicles = session.exec(select(Vehicle).where(Vehicle.base_id == req.base_id).order_by(Vehicle.plate)).all()

    # Filter drivers by requested or confirmed vehicle type
    allowed_types = set()
    if confirmation and confirmation.confirmed_vehicle_type:
        allowed_types = {t.strip().lower() for t in confirmation.confirmed_vehicle_type.split(",") if t.strip()}
    elif req.vehicle_type_requested:
        allowed_types = {t.strip().lower() for t in req.vehicle_type_requested.split(",") if t.strip()}

    if allowed_types:
        drivers = [
            d for d in drivers
            if not d.vehicle or d.vehicle.vehicle_type.lower() in allowed_types
        ]

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
            "confirmation": confirmation,
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
    vehicle_type_requested: list[str] = Form(...),
    cost_center: str = Form(default=""),
    reason: str = Form(default=""),
    notes: str = Form(default=""),
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    if len(vehicle_type_requested) != quantity:
        return _redirect(f"/partner?message={quote_plus('A quantidade de veículos e tipos de veículos deve ser igual.')}")

    vehicle_type_requested_str = ", ".join([v.strip() for v in vehicle_type_requested if v.strip()])
    try:
        payload = TravelRequestCreate(
            base_id=base_id,
            request_type=request_type,
            requested_datetime=parse_form_datetime(requested_datetime),
            origin=origin,
            destination=destination,
            quantity=quantity,
            vehicle_type_requested=vehicle_type_requested_str,
            cost_center=cost_center,
            reason=reason,
            notes=notes or None,
        )
        create_request(session, user, payload)
    except DomainError as exc:
        return _redirect(f"/partner?message={quote_plus(str(exc))}")

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
    vehicle_type_requested: list[str] = Form(...),
    cost_center: str = Form(default=""),
    reason: str = Form(default=""),
    notes: str = Form(default=""),
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")

    if len(vehicle_type_requested) != quantity:
        return _redirect(f"/partner?message={quote_plus('A quantidade de veículos e tipos de veículos deve ser igual.')}")

    vehicle_type_requested_str = ", ".join([v.strip() for v in vehicle_type_requested if v.strip()])
    try:
        payload = TravelRequestCreate(
            base_id=req.base_id,
            request_type=req.request_type,
            requested_datetime=parse_form_datetime(requested_datetime),
            origin=origin,
            destination=destination,
            quantity=quantity,
            vehicle_type_requested=vehicle_type_requested_str,
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
    confirmed_vehicle_type: list[str] = Form(default=[]),
    tariff_value: float = Form(default=0.0),
    observations: str = Form(default=""),
    refusal_reason: str = Form(default=""),
    driver_id: list[str] = Form(default=[]),
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")

    confirmed_vehicle_types_str = ""
    driver_ids_str = None
    first_driver_id_val = None

    if decision_type != DecisionType.REFUSE:
        if len(confirmed_vehicle_type) != approved_quantity:
            raise HTTPException(status_code=400, detail="A quantidade de tipos de veículos confirmados deve ser igual à quantidade aprovada.")
        
        valid_driver_ids = [d.strip() for d in driver_id if d.strip()]
        if len(valid_driver_ids) != approved_quantity:
            raise HTTPException(status_code=400, detail="Você deve vincular um motorista para cada veículo confirmado.")
        
        confirmed_vehicle_types_str = ", ".join([v.strip() for v in confirmed_vehicle_type if v.strip()])
        driver_ids_str = ", ".join(valid_driver_ids)
        first_driver_id_val = int(valid_driver_ids[0])

    try:
        payload = TriageDecisionPayload(
            decision_type=decision_type,
            approved_quantity=approved_quantity,
            confirmed_datetime=parse_form_datetime(confirmed_datetime),
            confirmed_vehicle_type=confirmed_vehicle_types_str,
            tariff_value=tariff_value,
            observations=observations or None,
            refusal_reason=refusal_reason or None,
            driver_id=first_driver_id_val,
            driver_ids=driver_ids_str,
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


@router.get("/requests/{request_id}/comprovante")
def download_comprovante(
    request_id: int,
    session: Session = Depends(get_session),
    user: User = Depends(login_required),
):
    """Retrieve the trip voucher (PDF) for a completed request.

    If the PDF is missing from the database or the disk, it will be regenerated
    on-the-fly. Checks permissions based on user role.
    """
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(status_code=404, detail="Pedido não encontrado")

    # Access control checks based on role
    if user.role == UserRole.PARTNER_REQUESTER:
        if req.company_id != user.company_id:
            raise HTTPException(status_code=403, detail="Sem permissão para este pedido")
    elif user.role == UserRole.BASE_SUPERVISOR:
        if not supervisor_can_access_request(session, user, req):
            raise HTTPException(status_code=403, detail="Sem permissão para este pedido")
    elif user.role in (UserRole.LOGISTICS_MANAGER, UserRole.FINANCE_READONLY):
        pass
    else:
        raise HTTPException(status_code=403, detail="Sem permissão")

    # Document retrieval and on-the-fly regeneration logic
    doc = session.exec(select(Document).where(Document.request_id == req.id)).first()
    file_exists = False
    if doc and doc.file_path:
        file_exists = Path(doc.file_path).exists()

    if not doc or not file_exists:
        # Regenerate document if missing
        doc = generate_pdf_document(session, req)

    return FileResponse(
        path=doc.file_path,
        media_type="application/pdf",
        filename=f"comprovante_{req.protocol}.pdf",
    )
