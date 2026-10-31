"""
auth_middleware.py — FastAPI dependency injection for JWT protection
--------------------------------------------------------------------
Usage:
    from auth_middleware import require_auth, require_admin

    @app.get("/api/protected")
    def protected(user: dict = Depends(require_auth)):
        user_id = user["uid"]
        ...
"""

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from auth import verify_access_token

_bearer = HTTPBearer(auto_error=False)


def get_token_from_request(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> Optional[str]:
    """
    Priority:
    1. Authorization: Bearer <token> header
    2. access_token HTTP-only cookie
    """
    if credentials and credentials.credentials:
        return credentials.credentials
    return request.cookies.get("access_token")


def require_auth(
    request: Request,
    token: Optional[str] = Depends(get_token_from_request),
) -> dict:
    """
    Returns decoded JWT payload with: sub (email), uid (int user_id), role.
    Raises 401 if missing or invalid.
    """
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please log in.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = verify_access_token(token)
    if not payload:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Session expired or invalid. Please log in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return payload


def require_super_admin(user: dict = Depends(require_auth)) -> dict:
    if user.get("role") != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super Admin access required.",
        )
    return user


def require_admin(user: dict = Depends(require_auth)) -> dict:
    if user.get("role") not in ("super_admin", "admin", "master_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required.",
        )
    return user


def require_master_admin(user: dict = Depends(require_auth)) -> dict:
    if user.get("role") not in ("super_admin", "admin", "master_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Master Admin role access required.",
        )
    return user


def optional_auth(
    token: Optional[str] = Depends(get_token_from_request),
) -> Optional[dict]:
    if not token:
        return None
    return verify_access_token(token)
