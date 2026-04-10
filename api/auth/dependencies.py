"""
Authentication dependencies for FastAPI routes.
Provides get_current_user() and role-based access control.
"""

from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger
from sqlalchemy.orm import Session

from api.auth.security import decode_token, verify_token_type
from database.connection import get_db
from database.models import User, UserRole

# HTTP Bearer token scheme (Authorization: Bearer <token>)
security = HTTPBearer()


# ---------------------------------------------------------------------------
# User Authentication Dependencies
# ---------------------------------------------------------------------------

async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """
    Dependency that validates JWT token and returns current user.
    
    Usage:
        @router.get("/protected")
        def protected_route(user: User = Depends(get_current_user)):
            return {"user_id": user.id, "email": user.email}
    
    Raises:
        HTTPException 401 if token is invalid or user not found
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    
    # Extract token from Authorization header
    token = credentials.credentials
    
    # Decode JWT
    payload = decode_token(token)
    if payload is None:
        raise credentials_exception
    
    # Verify token type is "access"
    if not verify_token_type(payload, "access"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type",
        )
    
    # Extract user ID from token
    user_id_str: Optional[str] = payload.get("sub")
    if user_id_str is None:
        raise credentials_exception
    
    try:
        user_id = UUID(user_id_str)
    except ValueError:
        raise credentials_exception
    
    # Fetch user from database
    user = db.query(User).filter(User.id == user_id).first()
    if user is None:
        raise credentials_exception
    
    # Check if user is active
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="User account is disabled",
        )
    
    return user


async def get_current_active_user(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    Dependency that ensures user is active (alias for get_current_user).
    Already checked in get_current_user, but kept for clarity.
    """
    return current_user


# ---------------------------------------------------------------------------
# Role-Based Access Control
# ---------------------------------------------------------------------------

def require_role(required_role: UserRole):
    """
    Factory function to create role-based dependencies.
    
    Usage:
        @router.delete("/admin/users/{user_id}")
        def delete_user(user: User = Depends(require_role(UserRole.ADMIN))):
            # Only admins can access this
    """
    async def role_checker(current_user: User = Depends(get_current_user)) -> User:
        if current_user.role != required_role:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {required_role.value}",
            )
        return current_user
    
    return role_checker


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """
    Dependency that requires admin role.
    
    Usage:
        @router.get("/admin/users")
        def list_all_users(user: User = Depends(require_admin)):
            # Only admins can access
    """
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return current_user


# ---------------------------------------------------------------------------
# Optional Authentication (for public routes that benefit from user context)
# ---------------------------------------------------------------------------

async def get_current_user_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(HTTPBearer(auto_error=False)),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """
    Optional authentication dependency.
    Returns User if valid token provided, None otherwise.
    Does not raise exception if no token.
    
    Usage:
        @router.get("/public/signals")
        def get_signals(user: Optional[User] = Depends(get_current_user_optional)):
            # Show user-specific signals if authenticated, public signals otherwise
            if user:
                return get_user_signals(user.id)
            return get_public_signals()
    """
    if credentials is None:
        return None
    
    try:
        token = credentials.credentials
        payload = decode_token(token)
        
        if payload is None or not verify_token_type(payload, "access"):
            return None
        
        user_id_str = payload.get("sub")
        if user_id_str is None:
            return None
        
        user_id = UUID(user_id_str)
        user = db.query(User).filter(User.id == user_id, User.is_active == True).first()
        
        return user
    except Exception as exc:
        logger.debug(f"Optional auth failed: {exc}")
        return None
