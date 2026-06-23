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


class ASGIPathFixMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            root_path = os.getenv("ROOT_PATH", "").rstrip("/")
            path = scope.get("path", "")
            if root_path and not path.startswith(root_path):
                scope["root_path"] = ""
        await self.app(scope, receive, send)


app = FastAPI(
    title="Central de Viagens Extras MVP",
    root_path=os.getenv("ROOT_PATH", "")
)
app.add_middleware(ASGIPathFixMiddleware)
app.add_middleware(SubdirMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "logtudo-viagens-extras-secret-key-change-me"),
    same_site="lax",
    https_only=False,
)
root_path = os.getenv("ROOT_PATH", "").rstrip("/")
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
if root_path:
    app.mount(f"{root_path}/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static_root")
app.include_router(web_router)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    log_file_path = os.path.join(os.path.dirname(__file__), "data", "routing.log")
    try:
        response = await call_next(request)
        status_code = response.status_code
    except Exception as e:
        status_code = 500
        raise e
    finally:
        try:
            with open(log_file_path, "a", encoding="utf-8") as f:
                f.write(
                    f"Method: {request.method} | "
                    f"Path: {request.url.path} | "
                    f"Raw Path: {request.scope.get('path')} | "
                    f"Root Path: {request.scope.get('root_path')} | "
                    f"Status: {status_code} | "
                    f"Headers: {dict(request.headers)}\n"
                )
        except Exception:
            pass
    return response


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)
