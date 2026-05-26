from fastapi.testclient import TestClient
import pytest

from app.main import app


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def login(client: TestClient, email: str, password: str):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)


def test_login_creates_session_and_redirects_by_role(client: TestClient):
    resp = login(client, "parceiro@logtudo.local", "parceiro123")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/partner"


def test_logout_invalidates_session(client: TestClient):
    login(client, "parceiro@logtudo.local", "parceiro123")
    out = client.post("/logout", follow_redirects=False)
    assert out.status_code == 303
    assert out.headers["location"] == "/login"

    private = client.get("/partner", follow_redirects=False)
    assert private.status_code == 303
    assert private.headers["location"] == "/login"


def test_requires_login_for_partner_and_company_areas(client: TestClient):
    client.post("/logout")
    r1 = client.get("/partner", follow_redirects=False)
    r2 = client.get("/empresa/operacoes", follow_redirects=False)

    assert r1.status_code == 303 and r1.headers["location"] == "/login"
    assert r2.status_code == 303 and r2.headers["location"] == "/login"


def test_partner_cannot_access_company_area(client: TestClient):
    login(client, "parceiro@logtudo.local", "parceiro123")
    resp = client.get("/empresa/operacoes")
    assert resp.status_code == 403


def test_finance_cannot_access_operations(client: TestClient):
    login(client, "financeiro@logtudo.local", "financeiro123")
    resp = client.get("/empresa/operacoes")
    assert resp.status_code == 403


def test_supervisor_cannot_access_finance(client: TestClient):
    login(client, "supervisor@logtudo.local", "supervisor123")
    resp = client.get("/empresa/financeiro")
    assert resp.status_code == 403


def test_manager_can_access_consolidated_views(client: TestClient):
    login(client, "gerente@logtudo.local", "gerente123")
    op = client.get("/empresa/operacoes")
    fin = client.get("/empresa/financeiro")
    mgr = client.get("/empresa/gerencial")

    assert op.status_code == 200
    assert fin.status_code == 200
    assert mgr.status_code == 200


def test_top_navigation_shows_only_allowed_links(client: TestClient):
    login(client, "parceiro@logtudo.local", "parceiro123")
    partner_page = client.get("/partner")
    assert "Portal Parceiro" in partner_page.text
    assert "Operações" not in partner_page.text
    assert "Financeiro" not in partner_page.text

    login(client, "gerente@logtudo.local", "gerente123")
    manager_page = client.get("/empresa/gerencial")
    assert "Operações" in manager_page.text
    assert "Financeiro" in manager_page.text
    assert "Gerencial" in manager_page.text
    assert "user_id=" not in manager_page.text
