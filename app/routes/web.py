import re
from datetime import timezone
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

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
from app.db import get_session
from app.models import (
    Base,
    DecisionType,
    Driver,
    DriverActivityStatus,
    OTPChallenge,
    RequestStatus,
    TriageDecisionPayload,
    TravelRequest,
    TravelRequestCreate,
    User,
    UserRole,
    Vehicle,
)
from app.services.datetime_utils import parse_form_datetime
from app.services.workflow import (
    DomainError,
    billing_csv,
    complete_trip,
    compute_sla,
    create_request,
    dispatch_trip,
    list_billable_requests,
    now_utc,
    request_otp,
    resend_otp,
    seed_data,
    sign_acceptance,
    triage_request,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
PHONE_E164_RE = re.compile(r"^\+[1-9]\d{1,14}$")


def _redirect(path: str):
    return RedirectResponse(url=path, status_code=303)


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
        if user.role == UserRole.BASE_SUPERVISOR and req.base_id != user.base_id:
            continue
        scoped.append(req)
    return scoped


def _base_context(session: Session, user: User) -> dict:
    scoped = _scoped_requests(session, user)
    bases = session.exec(select(Base)).all()
    sla_info = {}
    for req in scoped:
        base = next((b for b in bases if b.id == req.base_id), None)
        if base:
            sla_info[req.id] = compute_sla(req, base)

    counts = {
        "open": len([r for r in scoped if r.status in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE)]),
        "pending_acceptance": len([r for r in scoped if r.status == RequestStatus.CONFIRMED]),
        "in_execution": len([r for r in scoped if r.status == RequestStatus.IN_EXECUTION]),
        "completed": len([r for r in scoped if r.status == RequestStatus.COMPLETED]),
    }
    return {
        "travel_requests": scoped,
        "bases": {b.id: b for b in bases},
        "sla_info": sla_info,
        "counts": counts,
        "roles": UserRole,
        "status": RequestStatus,
        "driver_status": DriverActivityStatus,
        "can_access_finance": can_access_finance(user),
        "can_access_operations": can_access_operations(user),
    }


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)):
    seed_data(session)
    user = get_optional_user(request, session)
    if not user:
        return _redirect("/login")
    return _redirect(_role_home(user))


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, session: Session = Depends(get_session)):
    seed_data(session)
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
    return templates.TemplateResponse(
        "company_operations.html",
        {
            "request": request,
            "user": user,
            **ctx,
            "selected_request": None,
            "drivers_for_selected": [],
            "vehicles_for_selected": [],
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
    ctx = _base_context(session, user)
    items = list_billable_requests(session, None, None)
    return templates.TemplateResponse(
        "company_manager.html",
        {
            "request": request,
            "user": user,
            **ctx,
            "items": items,
            "title": "Empresa | Gerencial",
        },
    )


@router.get("/empresa/financeiro", response_class=HTMLResponse)
def company_finance(
    request: Request,
    session: Session = Depends(get_session),
):
    user = _require_roles_or_redirect(request, session, (UserRole.FINANCE_READONLY, UserRole.LOGISTICS_MANAGER))
    if isinstance(user, RedirectResponse):
        return user
    items = list_billable_requests(session, None, None)
    return templates.TemplateResponse(
        "company_finance.html",
        {
            "request": request,
            "user": user,
            "items": items,
            "title": "Empresa | Financeiro",
        },
    )


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
    if user.role == UserRole.BASE_SUPERVISOR and req.base_id != user.base_id:
        raise HTTPException(status_code=403, detail="Sem permissão para esta solicitação")

    drivers = session.exec(select(Driver).where(Driver.base_id == req.base_id).order_by(Driver.name)).all()
    vehicles = session.exec(select(Vehicle).where(Vehicle.base_id == req.base_id).order_by(Vehicle.plate)).all()
    return templates.TemplateResponse(
        "_supervisor_panel.html",
        {
            "request": request,
            "user": user,
            "selected_request": req,
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
def otp_send(request_id: int, session: Session = Depends(get_session), user: User = Depends(partner_only)):
    req = session.get(TravelRequest, request_id)
    if not req:
        raise HTTPException(404, "Pedido não encontrado")
    try:
        request_otp(session, user, req)
    except DomainError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    driver_id: int,
    status: DriverActivityStatus = Form(...),
    session: Session = Depends(get_session),
    user: User = Depends(supervisor_or_manager),
):
    driver = session.get(Driver, driver_id)
    if not driver:
        raise HTTPException(status_code=404, detail="Motorista não encontrado")
    if user.role == UserRole.BASE_SUPERVISOR and user.base_id != driver.base_id:
        raise HTTPException(status_code=403, detail="Sem permissão para este motorista")

    driver.activity_status = status
    driver.status_updated_at = now_utc()
    session.add(driver)
    session.commit()
    return _redirect("/empresa/operacoes")


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
