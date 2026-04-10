"""
Authentication routes — user registration, login, logout, token refresh.
"""

import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request
from loguru import logger
from pydantic import BaseModel, EmailStr, Field, validator
from sqlalchemy.orm import Session

from api.auth.dependencies import get_current_user
from api.auth.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
    verify_token_type,
)
from database.connection import get_db
from database.models import AuditLog, User, UserRole, UserRiskSettings

router = APIRouter()


# ---------------------------------------------------------------------------
# Request/Response Models
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    """User registration request."""
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=100)
    
    @validator("password")
    def password_strength(cls, v):
        """Validate password strength."""
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if not re.search(r"[A-Z]", v):
            raise ValueError("Password must contain at least one uppercase letter")
        if not re.search(r"[a-z]", v):
            raise ValueError("Password must contain at least one lowercase letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit")
        return v


class LoginRequest(BaseModel):
    """User login request."""
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    """Token refresh request."""
    refresh_token: str


class TokenResponse(BaseModel):
    """Authentication token response."""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = 3600  # seconds


class UserResponse(BaseModel):
    """User profile response."""
    id: str
    email: str
    role: str
    is_active: bool
    email_verified: bool
    created_at: str
    last_login_at: Optional[str]
    
    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(
    data: RegisterRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Register a new user account.
    
    **Password Requirements:**
    - Minimum 8 characters
    - At least one uppercase letter
    - At least one lowercase letter
    - At least one digit
    
    **Returns:**
    - User profile (without password)
    
    **Errors:**
    - 400: Email already registered or password too weak
    """
    # Check if email already exists
    existing_user = db.query(User).filter(User.email == data.email.lower()).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )
    
    # Hash password
    hashed_password = hash_password(data.password)
    
    # Create user
    new_user = User(
        email=data.email.lower(),
        hashed_password=hashed_password,
        role=UserRole.USER,  # Default role
        is_active=True,
        email_verified=False,  # Email verification can be added later
    )
    
    db.add(new_user)
    db.flush()  # Flush to get user ID before creating related records
    
    # Create default risk settings for new user
    default_risk_settings = UserRiskSettings(
        user_id=new_user.id,
        max_daily_drawdown_pct=3.0,
        max_weekly_drawdown_pct=7.0,
        max_risk_per_trade_pct=1.0,
        max_open_positions=5,
        default_lot_size=0.01,
        use_dynamic_sizing=True,
    )
    
    db.add(default_risk_settings)
    
    # Log registration event
    audit_log = AuditLog(
        user_id=new_user.id,
        action="user_registered",
        details={"email": data.email.lower()},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.add(audit_log)
    
    db.commit()
    db.refresh(new_user)
    
    logger.info(f"New user registered: {new_user.email} (ID: {new_user.id})")
    
    return UserResponse(
        id=str(new_user.id),
        email=new_user.email,
        role=new_user.role.value,
        is_active=new_user.is_active,
        email_verified=new_user.email_verified,
        created_at=new_user.created_at.isoformat(),
        last_login_at=None,
    )


@router.post("/login", response_model=TokenResponse)
def login(
    data: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Login and receive JWT tokens.
    
    **Returns:**
    - `access_token`: Short-lived token for API requests (1 hour)
    - `refresh_token`: Long-lived token for refreshing access tokens (7 days)
    
    **Errors:**
    - 401: Invalid credentials or inactive account
    """
    # Find user by email
    user = db.query(User).filter(User.email == data.email.lower()).first()
    
    # Verify user exists and password is correct
    if not user or not verify_password(data.password, user.hashed_password):
        # Log failed login attempt
        if user:
            audit_log = AuditLog(
                user_id=user.id,
                action="login_failed",
                details={"reason": "invalid_password"},
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
            )
            db.add(audit_log)
            db.commit()
        
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )
    
    # Check if user is active
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Account is disabled",
        )
    
    # Update last login timestamp
    user.last_login_at = datetime.now(timezone.utc)
    
    # Log successful login
    audit_log = AuditLog(
        user_id=user.id,
        action="login_success",
        details={},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    db.add(audit_log)
    db.commit()
    
    # Create JWT tokens
    access_token = create_access_token(data={"sub": str(user.id)})
    refresh_token = create_refresh_token(data={"sub": str(user.id)})
    
    logger.info(f"User logged in: {user.email} (ID: {user.id})")
    
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=3600,
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    data: RefreshRequest,
    db: Session = Depends(get_db),
):
    """
    Refresh access token using refresh token.
    
    **Use when access token expires** (after 1 hour) to get a new one without re-login.
    
    **Returns:**
    - New access_token and refresh_token
    
    **Errors:**
    - 401: Invalid or expired refresh token
    """
    # Decode refresh token
    payload = decode_token(data.refresh_token)
    
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )
    
    # Verify token type is "refresh"
    if not verify_token_type(payload, "refresh"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token type, expected refresh token",
        )
    
    # Extract user ID
    user_id_str = payload.get("sub")
    if user_id_str is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token payload",
        )
    
    # Verify user still exists and is active
    from uuid import UUID
    user = db.query(User).filter(User.id == UUID(user_id_str), User.is_active == True).first()
    
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found or inactive",
        )
    
    # Create new tokens
    new_access_token = create_access_token(data={"sub": user_id_str})
    new_refresh_token = create_refresh_token(data={"sub": user_id_str})
    
    logger.debug(f"Tokens refreshed for user: {user.email}")
    
    return TokenResponse(
        access_token=new_access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=3600,
    )


@router.post("/logout")
def logout(
    user: User = Depends(get_current_user),
    request: Request = None,
    db: Session = Depends(get_db),
):
    """
    Logout current user (client-side token deletion).
    
    **Note:** JWT tokens are stateless, so server-side logout just logs the event.
    Client must delete tokens from storage.
    
    **Future:** Implement token blacklist for immediate revocation if needed.
    """
    # Log logout event
    audit_log = AuditLog(
        user_id=user.id,
        action="logout",
        details={},
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )
    db.add(audit_log)
    db.commit()
    
    logger.info(f"User logged out: {user.email}")
    
    return {"status": "ok", "message": "Logged out successfully"}


@router.get("/me", response_model=UserResponse)
def get_me(user: User = Depends(get_current_user)):
    """
    Get current user profile.
    
    **Requires authentication** (Bearer token in Authorization header).
    
    **Returns:**
    - User profile information
    """
    return UserResponse(
        id=str(user.id),
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        email_verified=user.email_verified,
        created_at=user.created_at.isoformat(),
        last_login_at=user.last_login_at.isoformat() if user.last_login_at else None,
    )
