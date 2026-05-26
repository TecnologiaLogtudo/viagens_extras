import hashlib
import secrets
from typing import Iterable

from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session, select

from app.db import get_session
from app.models import User, UserRole

SESSION_KEY = "user_id"


def hash_password(password: str, salt: str | None = None) -> str:
    """Create a deterministic salted sha256 hash string: salt$hash."""
    salt = salt or secrets.token_hex(16)
    digest = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return f"{salt}${digest}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        salt, expected = stored_hash.split("$", 1)
    except ValueError:
        return False
    incoming = hashlib.sha256(f"{salt}:{password}".encode("utf-8")).hexdigest()
    return secrets.compare_digest(incoming, expected)


def authenticate_user(session: Session, email: str, password: str) -> User | None:
    user = session.exec(select(User).where(User.email == email, User.is_active == True)).first()
    if not user or not user.password_hash:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user


def _resolve_session_user(request: Request, session: Session) -> User | None:
    user_id = request.session.get(SESSION_KEY)
    if not user_id:
        return None
    return session.exec(select(User).where(User.id == int(user_id), User.is_active == True)).first()


def get_optional_user(request: Request, session: Session) -> User | None:
    return _resolve_session_user(request, session)


def get_current_user(request: Request, session: Session = Depends(get_session)) -> User:
    user = _resolve_session_user(request, session)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Não autenticado")
    return user


def login_required(request: Request, session: Session = Depends(get_session)) -> User:
    user = _resolve_session_user(request, session)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Não autenticado")
    return user


def require_roles(user: User, roles: Iterable[UserRole]) -> User:
    if user.role not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Sem permissão")
    return user


def partner_only(user: User = Depends(login_required)) -> User:
    return require_roles(user, [UserRole.PARTNER_REQUESTER])


def company_only(user: User = Depends(login_required)) -> User:
    return require_roles(
        user,
        [UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER, UserRole.FINANCE_READONLY],
    )


def supervisor_or_manager(user: User = Depends(company_only)) -> User:
    return require_roles(user, [UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER])


def finance_or_manager(user: User = Depends(company_only)) -> User:
    return require_roles(user, [UserRole.FINANCE_READONLY, UserRole.LOGISTICS_MANAGER])


def can_access_finance(user: User) -> bool:
    return user.role in (UserRole.FINANCE_READONLY, UserRole.LOGISTICS_MANAGER)


def can_access_operations(user: User) -> bool:
    return user.role in (UserRole.BASE_SUPERVISOR, UserRole.LOGISTICS_MANAGER)
