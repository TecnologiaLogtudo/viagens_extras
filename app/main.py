from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.sessions import SessionMiddleware
from sqlmodel import Session

from app.db import engine, init_db
from app.routes.web import router as web_router
from app.services.bootstrap import seed_runtime_data

load_dotenv()

app = FastAPI(title="Central de Viagens Extras MVP")
app.add_middleware(
    SessionMiddleware,
    secret_key="logtudo-viagens-extras-secret-key-change-me",
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(web_router)


@app.on_event("startup")
def on_startup():
    init_db()
    with Session(engine) as session:
        seed_runtime_data(session)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)
