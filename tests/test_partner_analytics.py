from fastapi.testclient import TestClient
import pytest
from datetime import datetime, timezone
from sqlmodel import Session, select

from app.db import engine
from app.models import TravelRequest, Company, CompanyBase, RequestStatus, User, Base
from tests.test_web_auth import login


def test_partner_analytics_unauthorized(client: TestClient):
    # Try calling without logging in
    resp = client.get("/partner/analytics/data")
    assert resp.status_code in (303, 401)


def test_partner_analytics_data_and_export(client: TestClient):
    # Log in as partner
    login(client, "parceiro@logtudo.local", "parceiro123")
    
    # Check that we can get data successfully
    resp = client.get("/partner/analytics/data")
    assert resp.status_code == 200
    
    payload = resp.json()
    assert "metrics" in payload
    assert "charts" in payload
    assert "requests" in payload
    assert "available_years" in payload
    
    metrics = payload["metrics"]
    assert "total_requests" in metrics
    assert "quote_conversion_rate" in metrics
    
    charts = payload["charts"]
    assert "status_distribution" in charts
    assert "monthly_volume" in charts
    assert "base_distribution" in charts

    # Check export endpoint
    export_resp = client.get("/partner/analytics/export")
    assert export_resp.status_code == 200
    assert "text/csv" in export_resp.headers["content-type"]
    assert "attachment; filename=" in export_resp.headers["content-disposition"]
    
    content = export_resp.text
    assert "Protocolo;" in content
    assert "Solicitado em;" in content


def test_partner_analytics_filtering(client: TestClient):
    login(client, "parceiro@logtudo.local", "parceiro123")
    
    with Session(engine) as session:
        # Let's find the company and base of the test partner
        partner = session.exec(select(User).where(User.email == "parceiro@logtudo.local")).first()
        base = session.exec(select(Base)).first()
        
        # Create a mock TravelRequest for a specific date and status
        req = TravelRequest(
            protocol="TR-TEST-ANALYTICS",
            company_id=partner.company_id,
            base_id=base.id,
            requested_by_user_id=partner.id,
            request_type="Viagem extra NILO",
            requested_datetime=datetime(2025, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
            origin="A",
            destination="B",
            quantity=1,
            vehicle_type_requested="sedan",
            cost_center="TEST",
            reason="Test",
            status=RequestStatus.COMPLETED
        )
        session.add(req)
        session.commit()
        session.refresh(req)
        req_id = req.id

    try:
        # Filter for year 2025 and month 6 (June)
        resp = client.get("/partner/analytics/data?year=2025&month=6")
        assert resp.status_code == 200
        payload = resp.json()
        
        # Ensure our test request is returned
        requests = payload["requests"]
        test_reqs = [r for r in requests if r["protocol"] == "TR-TEST-ANALYTICS"]
        assert len(test_reqs) == 1
        assert test_reqs[0]["status"] == "completed"
        
        # Filter for status completed
        resp_status = client.get("/partner/analytics/data?status=completed")
        assert resp_status.status_code == 200
        payload_status = resp_status.json()
        assert len([r for r in payload_status["requests"] if r["protocol"] == "TR-TEST-ANALYTICS"]) == 1

        # Filter for status canceled (should not contain our test request)
        resp_canceled = client.get("/partner/analytics/data?status=canceled")
        assert resp_canceled.status_code == 200
        payload_canceled = resp_canceled.json()
        assert len([r for r in payload_canceled["requests"] if r["protocol"] == "TR-TEST-ANALYTICS"]) == 0

    finally:
        # Clean up
        with Session(engine) as session:
            req_db = session.get(TravelRequest, req_id)
            if req_db:
                session.delete(req_db)
                session.commit()
