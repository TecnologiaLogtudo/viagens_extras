import os
import re
from fastapi import FastAPI, Response, Request
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from sqlmodel import Session

from app.db import engine, init_db
from app.routes.web import router as web_router


load_dotenv()


class SubdirMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        root_path = os.getenv("ROOT_PATH", "").rstrip("/")
        if not root_path:
            return await call_next(request)

        response = await call_next(request)

        # 1. Handle Redirects
        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "")
            if location.startswith("/") and not location.startswith(root_path + "/"):
                response.headers["location"] = root_path + location

        # 2. Handle HTML links rewriting
        content_type = response.headers.get("content-type", "")
        if "text/html" in content_type:
            body = b""
            async for chunk in response.body_iterator:
                body += chunk
            
            html = body.decode("utf-8")
            
            def repl(match):
                attr = match.group(1)
                path = match.group(2)
                if path.startswith("http://") or path.startswith("https://") or path.startswith("//"):
                    return match.group(0)
                clean_root = root_path.lstrip("/")
                if path.startswith(clean_root):
                    return match.group(0)
                return f'{attr}="{root_path}/{path}"'
                
            pattern = r'(href|src|action|hx-get|hx-post)="/([^"]*)"'
            html = re.sub(pattern, repl, html)
            
            modified_body = html.encode("utf-8")
            response.headers["content-length"] = str(len(modified_body))
            
            async def new_body_iterator():
                yield modified_body
                
            response.body_iterator = new_body_iterator()

        return response


app = FastAPI(
    title="Central de Viagens Extras MVP",
    root_path=os.getenv("ROOT_PATH", "")
)
app.add_middleware(SubdirMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "logtudo-viagens-extras-secret-key-change-me"),
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
app.include_router(web_router)


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)
