import re
import csv
from io import StringIO
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, Query, HTTPException, Response
from fastapi.responses import JSONResponse
from sqlmodel import Session, select
from zoneinfo import ZoneInfo

from app.db import get_session
from app.auth import partner_only
from app.models import User, TravelRequest, Base, OperationalConfirmation, RequestStatus
from app.services.workflow import ensure_aware

router = APIRouter(prefix="/partner/analytics", tags=["partner_analytics"])


def local_dt(dt: datetime | None) -> datetime | None:
    if not dt:
        return None
    return ensure_aware(dt).astimezone(ZoneInfo("America/Sao_Paulo"))


def _scoped_requests(session: Session, user: User):
    requests = session.exec(select(TravelRequest).order_by(TravelRequest.created_at.desc())).all()
    scoped = []
    for req in requests:
        if req.company_id == user.company_id:
            scoped.append(req)
    return scoped


@router.get("/data")
def get_analytics_data(
    month: Optional[int] = Query(None),
    year: Optional[int] = Query(None),
    base_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    requests = _scoped_requests(session, user)
    bases = session.exec(select(Base)).all()
    bases_map = {b.id: b for b in bases}

    # Available years for filter (always includes the current year dynamically)
    current_year = datetime.now(ZoneInfo("America/Sao_Paulo")).year
    years_set = {local_dt(r.requested_datetime).year for r in requests if r.requested_datetime}
    years_set.add(current_year)
    available_years = sorted(list(years_set), reverse=True)

    # Filtering
    filtered = []
    for r in requests:
        local_req_dt = local_dt(r.requested_datetime)
        if year and local_req_dt.year != year:
            continue
        if month and local_req_dt.month != month:
            continue
        if base_id and r.base_id != base_id:
            continue
        if status:
            if status == "open":
                if r.status not in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE, RequestStatus.ACCEPTED, RequestStatus.IN_EXECUTION):
                    continue
            elif status == "pending_acceptance":
                if not (r.status == RequestStatus.CONFIRMED and r.request_type == "Cotação de preço"):
                    continue
            elif status == "completed":
                if r.status != RequestStatus.COMPLETED:
                    continue
            elif status == "canceled":
                if r.status not in (RequestStatus.CANCELED, RequestStatus.REFUSED):
                    continue
        filtered.append(r)

    # Status labels mapping
    status_labels_map = {
        RequestStatus.SUBMITTED: "Enviado",
        RequestStatus.TRIAGE: "Em triagem",
        RequestStatus.CONFIRMED: "Aprovação pendente de aceite",
        RequestStatus.ACCEPTED: "Aceito",
        RequestStatus.IN_EXECUTION: "Em execução",
        RequestStatus.COMPLETED: "Concluído",
        RequestStatus.REFUSED: "Recusado",
        RequestStatus.CANCELED: "Cancelado",
    }

    # 1. Metrics KPIs
    total_requests = len(filtered)
    
    # Quote conversion rate
    quotes = [r for r in filtered if r.request_type == "Cotação de preço"]
    quotes_converted = [q for q in quotes if q.status == RequestStatus.COMPLETED]
    quote_conversion_rate = (len(quotes_converted) / len(quotes) * 100) if quotes else 0.0

    # 2. Charts Data
    # Status Distribution grouped into the 4 main states
    status_groups = {
        "Abertos": 0,
        "Aguardando aceite": 0,
        "Concluídos": 0,
        "Cancelados": 0
    }
    for r in filtered:
        if r.status in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE, RequestStatus.ACCEPTED, RequestStatus.IN_EXECUTION):
            status_groups["Abertos"] += 1
        elif r.status == RequestStatus.CONFIRMED and r.request_type == "Cotação de preço":
            status_groups["Aguardando aceite"] += 1
        elif r.status == RequestStatus.COMPLETED:
            status_groups["Concluídos"] += 1
        elif r.status in (RequestStatus.CANCELED, RequestStatus.REFUSED):
            status_groups["Cancelados"] += 1
            
    status_distribution = {
        "labels": [k for k, v in status_groups.items() if v > 0],
        "data": [v for k, v in status_groups.items() if v > 0]
    }

    # Base Distribution
    base_counts = {}
    for r in filtered:
        b = bases_map.get(r.base_id)
        b_lbl = f"{b.name} - {b.location}" if b else "N/A"
        base_counts[b_lbl] = base_counts.get(b_lbl, 0) + 1

    # Monthly volume for the selected year
    selected_year = year or datetime.now(ZoneInfo("America/Sao_Paulo")).year
    month_counts = [0] * 12
    for r in requests:
        local_r_dt = local_dt(r.requested_datetime)
        if local_r_dt and local_r_dt.year == selected_year:
            month_counts[local_r_dt.month - 1] += 1

    months_labels = ["Jan", "Fev", "Mar", "Abr", "Mai", "Jun", "Jul", "Ago", "Set", "Out", "Nov", "Dez"]

    # 3. Requests List
    requests_list = []
    for r in filtered:
        b = bases_map.get(r.base_id)
        base_name = f"{b.name} - {b.location}" if b else "N/A"
        created_str = local_dt(r.created_at).strftime("%d/%m/%Y %H:%M") if r.created_at else "N/A"
        req_dt_str = local_dt(r.requested_datetime).strftime("%d/%m/%Y %H:%M") if r.requested_datetime else "N/A"
        requests_list.append({
            "id": r.id,
            "protocol": r.protocol,
            "created_at": created_str,
            "requested_datetime": req_dt_str,
            "request_type": r.request_type,
            "status": r.status.value,
            "status_label": status_labels_map.get(r.status) or r.status.value,
            "base_name": base_name,
            "origin": r.origin,
            "destination": r.destination,
            "quantity": r.quantity,
        })

    return {
        "metrics": {
            "total_requests": total_requests,
            "quote_conversion_rate": round(quote_conversion_rate, 1),
            "quotes_total": len(quotes),
            "quotes_converted": len(quotes_converted),
        },
        "charts": {
            "status_distribution": status_distribution,
            "monthly_volume": {
                "labels": months_labels,
                "data": month_counts,
            },
            "base_distribution": {
                "labels": list(base_counts.keys()),
                "data": list(base_counts.values()),
            }
        },
        "available_years": available_years,
        "requests": requests_list,
    }


@router.get("/export")
def export_analytics_csv(
    month: Optional[int] = Query(None),
    year: Optional[int] = Query(None),
    base_id: Optional[int] = Query(None),
    status: Optional[str] = Query(None),
    session: Session = Depends(get_session),
    user: User = Depends(partner_only),
):
    requests = _scoped_requests(session, user)
    bases = session.exec(select(Base)).all()
    bases_map = {b.id: b for b in bases}

    # Filtering
    filtered = []
    for r in requests:
        local_req_dt = local_dt(r.requested_datetime)
        if year and local_req_dt.year != year:
            continue
        if month and local_req_dt.month != month:
            continue
        if status:
            if status == "open":
                if r.status not in (RequestStatus.SUBMITTED, RequestStatus.TRIAGE, RequestStatus.ACCEPTED, RequestStatus.IN_EXECUTION):
                    continue
            elif status == "pending_acceptance":
                if not (r.status == RequestStatus.CONFIRMED and r.request_type == "Cotação de preço"):
                    continue
            elif status == "completed":
                if r.status != RequestStatus.COMPLETED:
                    continue
            elif status == "canceled":
                if r.status not in (RequestStatus.CANCELED, RequestStatus.REFUSED):
                    continue
        filtered.append(r)

    status_labels_map = {
        RequestStatus.SUBMITTED: "Enviado",
        RequestStatus.TRIAGE: "Em triagem",
        RequestStatus.CONFIRMED: "Aprovação pendente de aceite",
        RequestStatus.ACCEPTED: "Aceito",
        RequestStatus.IN_EXECUTION: "Em execução",
        RequestStatus.COMPLETED: "Concluído",
        RequestStatus.REFUSED: "Recusado",
        RequestStatus.CANCELED: "Cancelado",
    }

    f = StringIO()
    f.write('\ufeff')  # UTF-8 BOM for Excel
    writer = csv.writer(f, delimiter=';')
    writer.writerow([
        "Protocolo", "Solicitado em", "Solicitante", "Data Agendamento", "Tipo", 
        "Status", "Base", "Origem", "Destino", "Qtd. Veiculos", 
        "Tipo Veiculos", "Centro de Custo", "Observações"
    ])

    for r in filtered:
        b = bases_map.get(r.base_id)
        base_name = f"{b.name} - {b.location}" if b else "N/A"
        created_str = local_dt(r.created_at).strftime("%d/%m/%Y %H:%M") if r.created_at else "N/A"
        req_dt_str = local_dt(r.requested_datetime).strftime("%d/%m/%Y %H:%M") if r.requested_datetime else "N/A"
        st_lbl = status_labels_map.get(r.status) or r.status.value

        requester = session.get(User, r.requested_by_user_id) if r.requested_by_user_id else None
        requester_name = requester.full_name if requester else "N/A"

        writer.writerow([
            r.protocol,
            created_str,
            requester_name,
            req_dt_str,
            r.request_type,
            st_lbl,
            base_name,
            r.origin,
            r.destination,
            r.quantity,
            r.vehicle_type_requested,
            r.cost_center or "",
            r.notes or ""
        ])

    f.seek(0)
    response = Response(content=f.getvalue(), media_type="text/csv")
    response.headers["Content-Disposition"] = f"attachment; filename=relatorio_solicitacoes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return response
